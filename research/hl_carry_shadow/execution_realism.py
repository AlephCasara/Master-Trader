#!/usr/bin/env python3
"""Execution-realism audit of the carry shadow archive (issue #27).

Read-only and offline: no network, no writes to SOURCE or REPLAY. Quantifies
what the frozen paper model (tracker.py) does not charge:

  1. break-even extra cost per fill, against zero and the 9% / 12% hurdles;
  2. fee tier: study Binance spot 7.5 bps vs the base-tier 10 bps;
  3. top-of-book spread and depth at every recorded hourly decision and at
     every modeled leg, against the same-base-quantity requirement;
  4. cross-venue basis and the noise added by non-atomic quote capture;
  5. funding on marked notional instead of a fixed $1,000;
  6. the trade-through fill proxy (how far price traded through, queue ahead);
  7. optional: beyond-touch impact from a forward public-depth sample.

    python3 execution_realism.py SOURCE REPLAY [FORWARD_EPISODES] [--depth-table T.json]

SOURCE is the shadow directory (data/bbo_*.jsonl[.gz], data/funding.jsonl,
monitor.log). REPLAY is the corrected replay directory (data/episodes.jsonl,
summary.json). The optional FORWARD_EPISODES file adds post-replay fills.
Prints one JSON document. Aggregates only; no account data is involved.
"""
import argparse
import gzip
import json
import math
import statistics
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ft_userdata.analysis.costs import (  # noqa: E402
    FundingSeries, book_impact_bps, fee_bps, funding_cost,
)

UTC = timezone.utc
NOTIONAL = 1000.0           # tracker.NOTIONAL (frozen)
CAPITAL_PER_EPISODE = 1500  # replay.py matched capital-time denominator
FILLS_PER_EPISODE = 4
STUDY_FEES_BPS = {"hl_entry_maker": 1.5, "hl_exit_taker": 4.5, "bn_taker": 7.5}  # tracker.py, frozen
DECISION_STALE_S = 120      # tracker/replay: latest quote must be <= 120 s old
# Order in which monitor.py polls l2Book after one Binance batch call. A coin's
# HL quote is captured roughly k * (0.15 s pace + RTT) after the Binance quote.
UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC", "BCH", "NEAR",
            "SUI", "UNI", "TRX", "HBAR", "ENA", "TAO", "ARB", "ZEC", "BNB", "WLD", "PUMP", "JTO",
            "AAVE", "XPL", "KAITO", "CRV", "JUP", "VIRTUAL", "PAXG", "TRB", "ZRO", "TRUMP",
            "MORPHO", "ETHFI", "APT", "ASTER", "ONDO", "EIGEN", "GRAM"]
L2_PACE_S = 0.15
PERIODS = [("pre-fleet-v2", None, "2026-08-23"), ("fleet-v2-429-window", "2026-08-23", "2026-09-14"),
           ("post-correction", "2026-09-14", None)]


# ---------------------------------------------------------------------------
# pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def pct(values, q):
    v = sorted(x for x in values if x is not None and math.isfinite(x))
    if not v:
        return None
    i = (len(v) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def dist(values):
    v = [x for x in values if x is not None and math.isfinite(x)]
    return {"n": len(v), "p10": pct(v, .1), "p50": pct(v, .5), "p90": pct(v, .9),
            "max": max(v) if v else None}


def spread_bps(bid, ask):
    if not bid or not ask or bid <= 0 or ask < bid:
        return None
    return (ask - bid) / ((ask + bid) / 2) * 1e4


def break_even(net_usd, episodes, position_hours, hurdles=(0.0, 0.09, 0.12)):
    """Extra cost per fill (bps of $1,000) that takes the replay to each hurdle
    of annualized return on matched deployed capital-time."""
    fills = episodes * FILLS_PER_EPISODE
    out = {}
    for h in hurdles:
        required = h * position_hours * CAPITAL_PER_EPISODE / 8760
        out[f"{h:.0%}"] = {"required_net_usd": required,
                           "extra_bps_per_fill": (net_usd - required) / (fills * NOTIONAL) * 1e4}
    return out


def annualized(net_usd, position_hours):
    return net_usd / (position_hours * CAPITAL_PER_EPISODE) * 8760 * 100 if position_hours else None


def decision_for_quote(decisions, ts):
    """Index of the first decision that may use a quote stamped ``ts``: the
    decision is not earlier than the quote and at most DECISION_STALE_S after
    it (tracker/replay rule). Keeping the latest such quote per coin gives the
    snapshot the tracker used. None if no decision qualifies."""
    i = bisect_left(decisions, ts)
    if i < len(decisions) and 0 <= decisions[i] - ts <= DECISION_STALE_S:
        return i
    return None


def leg_metrics(row, qty):
    """Spread, top-of-book coverage and recorded impact for one modeled leg.

    HL has two recorded levels, Binance one. Where the recorded levels cannot
    fill ``qty``, impact is a LOWER BOUND: the remainder is priced at the last
    recorded level (HL) or at the touch (Binance)."""
    def lv(px_key, sz_key):
        px, sz = row.get(px_key), row.get(sz_key)
        return (px, sz) if px and sz else None

    def nxt(key):
        v = row.get(key)
        return (v[0], v[1]) if v and v[0] and v[1] else None

    hl_asks = [x for x in (lv("hl_ask", "hl_ask_sz"), nxt("hl_ask2")) if x]
    out = {"hl_spread_bps": spread_bps(row.get("hl_bid"), row.get("hl_ask")),
           "bn_spread_bps": spread_bps(row.get("bn_bid"), row.get("bn_ask")),
           "required_base_qty": qty}
    for name, levels in (("hl_buy", hl_asks), ("bn_buy", [x for x in [lv("bn_ask", "bn_ask_sz")] if x]),
                         ("bn_sell", [x for x in [lv("bn_bid", "bn_bid_sz")] if x])):
        if not levels:
            out[name] = None
            continue
        depth = sum(sz for _, sz in levels)
        full = book_impact_bps(levels, qty * levels[0][0], "buy" if name.endswith("buy") else "sell")
        if full is None:  # extend the last recorded level to bound impact from below
            ext = levels[:-1] + [(levels[-1][0], qty)]
            full = book_impact_bps(ext, qty * levels[0][0], "buy" if name.endswith("buy") else "sell")
            bound = True
        else:
            bound = False
        out[name] = {"recorded_levels": len(levels), "l1_coverage": levels[0][1] / qty,
                     "recorded_coverage": depth / qty, "impact_bps": full,
                     "impact_is_lower_bound": bound}
    return out


def hedge_drift(series, t, lags=(-120, -60, 60, 120, 300)):
    """Binance mid change (bps) from the snapshot at ``t`` to each lag, from a
    sorted [(ts, hl_mid, bn_mid, hl_mark)] series; missing points are skipped."""
    times = [x[0] for x in series]
    k = bisect_left(times, t)
    if k >= len(series) or series[k][0] != t or not series[k][2]:
        return {}
    base = series[k][2]
    out = {}
    for lag in lags:
        j = bisect_left(times, t + lag)
        if j < len(series) and abs(series[j][0] - (t + lag)) <= 30 and series[j][2]:
            out[lag] = (series[j][2] - base) / base * 1e4
    return out


def funding_fetch_failures(log_text):
    """(hour_start_epoch_s, coin) pairs whose hourly settled-funding fetch failed."""
    out = set()
    for line in log_text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "fundingHistory" and parts[3:4] == ["failed:"]:
            ts = datetime.fromisoformat(parts[0]).timestamp()
            out.add((int(ts // 3600 * 3600), parts[2]))
    return out


def skew_noise_bps(sigma_1m_bps, index, rtt_s):
    """Std-dev of the HL-minus-Binance price gap created by capturing coin
    ``index``'s HL quote later than the batched Binance quote (random walk)."""
    lag = index * (L2_PACE_S + rtt_s) + rtt_s
    return sigma_1m_bps * math.sqrt(lag / 60.0)


# ---------------------------------------------------------------------------
# archive loading
# ---------------------------------------------------------------------------
def read_jsonl(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def decision_times(source):
    out = set()
    for line in (Path(source) / "monitor.log").read_text().splitlines():
        if " tracker:" in line:
            out.add(datetime.fromisoformat(line.split()[0]).timestamp())
    return sorted(out)


def load_archive(source, decisions, traded, leg_keys):
    """One pass over the BBO archive. Returns per-coin compact mid series for
    traded coins, the rows needed for legs, and decision snapshots."""
    series = defaultdict(list)     # coin -> [(ts, hl_mid, bn_mid, hl_mark)]
    leg_rows = {}
    snap = {}                      # (decision_idx, coin) -> row (latest qualifying)
    files = sorted((Path(source) / "data").glob("bbo_*.jsonl*"))
    for fp in files:
        for r in read_jsonl(fp):
            coin, ts = r.get("coin"), r.get("ts")
            if coin is None or ts is None:
                continue
            if coin in traded:
                hb, ha, bb, ba = r.get("hl_bid"), r.get("hl_ask"), r.get("bn_bid"), r.get("bn_ask")
                series[coin].append((ts, (hb + ha) / 2 if hb and ha else None,
                                     (bb + ba) / 2 if bb and ba else None, r.get("hl_mark")))
            if (coin, ts) in leg_keys:
                leg_rows[(coin, ts)] = r
            i = decision_for_quote(decisions, ts)
            if i is not None:
                cur = snap.get((i, coin))
                if cur is None or ts >= cur["ts"]:
                    snap[(i, coin)] = r
    for c in series:
        series[c].sort()
    return series, leg_rows, snap


def period_of(ts):
    day = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
    for name, start, end in PERIODS:
        if (start is None or day >= start) and (end is None or day < end):
            return name
    return "unknown"


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------
def analyse(source, replay, forward=None, depth_table=None, rtts=(0.05, 0.30)):
    source, replay = Path(source), Path(replay)
    summary = json.loads((replay / "summary.json").read_text())
    events = list(read_jsonl(replay / "data" / "episodes.jsonl"))
    fwd = [e for e in (read_jsonl(forward) if forward else [])
           if e.get("accounting_version") == 2 and e.get("ep_id") not in {x.get("ep_id") for x in events}]
    all_events = events + fwd
    signals = {e["ep_id"]: e for e in all_events if e["event"] == "signal"}
    fills = [e for e in all_events if e["event"] == "fill"]
    exits = [e for e in events if e["event"] == "exit"]
    traded = sorted({e["coin"] for e in fills})
    decisions = decision_times(source)

    leg_keys = set()
    for e in fills:
        leg_keys.add((e["coin"], e["fill_ts"]))
    for e in exits:
        leg_keys.add((e["coin"], e["exit_ts"]))
    series, leg_rows, snaps = load_archive(source, decisions, set(traded), leg_keys)

    # signal-time quote (queue ahead of the posted ask) from the decision snapshot
    # Live decisions take `now` before the funding fetch and log ~20 s later;
    # the replay used the log time. Either maps to the same minute snapshot.
    def snap_at(coin, t):
        i = bisect_left(decisions, t)
        for j in (i, i - 1):
            if 0 <= j < len(decisions) and abs(decisions[j] - t) < 60:
                return snaps.get((j, coin))
        return None

    # ---- 1/2. break-even and fee tier -----------------------------------
    net, n_ep, hours = summary["net_model_usd"], summary["closed_episodes"], summary["completed_position_hours"]
    base_bn = fee_bps("binance", "spot", "taker")
    base_hl_maker = fee_bps("hyperliquid", "perp", "maker")
    base_hl_taker = fee_bps("hyperliquid", "perp", "taker")
    study_ep_bps = STUDY_FEES_BPS["hl_entry_maker"] + STUDY_FEES_BPS["hl_exit_taker"] + 2 * STUDY_FEES_BPS["bn_taker"]
    base_ep_bps = base_hl_maker + base_hl_taker + 2 * base_bn
    taker_ep_bps = 2 * base_hl_taker + 2 * base_bn
    fee_scen = {}
    for name, ep_bps in (("study_fees", study_ep_bps), ("base_tier_fees", base_ep_bps),
                         ("base_tier_hl_taker_entry", taker_ep_bps)):
        n = net - n_ep * NOTIONAL * (ep_bps - study_ep_bps) / 1e4
        fee_scen[name] = {"fees_bps_per_episode": ep_bps, "net_usd": n,
                          "annualized_matched_pct": annualized(n, hours)}

    # ---- 3. legs ---------------------------------------------------------
    legs = []
    by_ep_fill = {e["ep_id"]: e for e in fills}
    for e in fills:
        qty = NOTIONAL / e["perp_entry"]
        row = leg_rows.get((e["coin"], e["fill_ts"]))
        sig = signals.get(e["ep_id"])
        post = snap_at(e["coin"], sig["ts"] / 1000) if sig else None
        m = leg_metrics(row, qty) if row else None
        through = ((row["hl_bid"] - e["perp_entry"]) / e["perp_entry"] * 1e4
                   if row and row.get("hl_bid") else None)
        # Binance mid around the fill snapshot. Trade-through fills are selected
        # on a rising HL bid, so the hedge price is moving when the model buys
        # it; the drift prices hedge latency (and the cross-to-snapshot gap).
        drift = hedge_drift(series.get(e["coin"], []), e["fill_ts"])
        nxt = drift.get(60)
        legs.append({"ep_id": e["ep_id"], "coin": e["coin"], "event": "entry",
                     "period": period_of(e["fill_ts"]), "replay": e in events,
                     "fill_delay_h": e.get("fill_delay_h"),
                     "hl_maker_queue_ahead_qty": post.get("hl_ask_sz") if post else None,
                     "hl_maker_qty_over_queue": (qty / post["hl_ask_sz"]) if post and post.get("hl_ask_sz") else None,
                     "trade_through_bps": through,
                     "bn_mid_next_minute_bps": nxt,
                     "bn_mid_drift_bps": drift,
                     "entry_exec_basis_bps": (e["perp_entry"] - e["spot_entry"]) / e["spot_entry"] * 1e4,
                     "bn_buy": m and m["bn_buy"], "hl_spread_bps": m and m["hl_spread_bps"],
                     "bn_spread_bps": m and m["bn_spread_bps"]})
    for e in exits:
        qty = NOTIONAL / by_ep_fill[e["ep_id"]]["perp_entry"]
        row = leg_rows.get((e["coin"], e["exit_ts"]))
        m = leg_metrics(row, qty) if row else None
        legs.append({"ep_id": e["ep_id"], "coin": e["coin"], "event": "exit",
                     "period": period_of(e["exit_ts"]), "replay": True,
                     "exit_exec_basis_bps": (e["perp_exit"] - e["spot_exit"]) / e["spot_exit"] * 1e4,
                     "hl_buy": m and m["hl_buy"], "bn_sell": m and m["bn_sell"],
                     "hl_spread_bps": m and m["hl_spread_bps"], "bn_spread_bps": m and m["bn_spread_bps"]})

    def taker_legs(key):
        return [x[key] for x in legs if x.get(key)]
    taker = {k: {"n": len(taker_legs(k)),
                 "l1_insufficient": sum(v["l1_coverage"] < 1 for v in taker_legs(k)),
                 "recorded_insufficient": sum(v["impact_is_lower_bound"] for v in taker_legs(k)),
                 "impact_bps_lower_bound": dist([v["impact_bps"] for v in taker_legs(k)]),
                 "l1_coverage": dist([v["l1_coverage"] for v in taker_legs(k)])}
             for k in ("bn_buy", "hl_buy", "bn_sell")}
    replay_extra_usd = sum(NOTIONAL * v["impact_bps"] / 1e4
                           for x in legs if x["replay"] for k in ("bn_buy", "hl_buy", "bn_sell")
                           if (v := x.get(k)))

    # ---- 3b. decision-time conditions -----------------------------------
    cond = defaultdict(lambda: defaultdict(list))
    hl_missing = defaultdict(lambda: [0, 0])
    for (i, coin), r in snaps.items():
        per = period_of(decisions[i])
        hl_missing[per][1] += 1
        if not (r.get("hl_bid") and r.get("hl_ask")):
            hl_missing[per][0] += 1
        scope = "traded" if coin in traded else "other"
        for sc in (scope, "all"):
            c = cond[sc]
            c["hl_spread_bps"].append(spread_bps(r.get("hl_bid"), r.get("hl_ask")))
            c["bn_spread_bps"].append(spread_bps(r.get("bn_bid"), r.get("bn_ask")))
            for side, px, sz in (("hl_ask", "hl_ask", "hl_ask_sz"), ("bn_ask", "bn_ask", "bn_ask_sz"),
                                 ("bn_bid", "bn_bid", "bn_bid_sz")):
                v = r.get(px) * r.get(sz) if r.get(px) and r.get(sz) else None
                c[f"{side}_l1_usd"].append(v)
    decision_conditions = {}
    for sc, c in cond.items():
        decision_conditions[sc] = {k: dist(v) for k, v in c.items()}
        for side in ("hl_ask", "bn_ask", "bn_bid"):
            vals = [v for v in c[f"{side}_l1_usd"] if v is not None]
            decision_conditions[sc][f"{side}_l1_ge_1000_share"] = (
                sum(v >= NOTIONAL for v in vals) / len(vals) if vals else None)
    missing = {p: {"missing": a, "snapshots": b, "share": a / b if b else None}
               for p, (a, b) in sorted(hl_missing.items())}

    # ---- 4. basis and non-atomic capture noise --------------------------
    basis = {}
    for coin in traded:
        s = series.get(coin, [])
        mids = [(t, h) for t, h, _, _ in s if h]
        rets = [math.log(b[1] / a[1]) * 1e4 for a, b in zip(mids, mids[1:]) if 0 < b[0] - a[0] <= 61]
        sig1 = statistics.pstdev(rets) if len(rets) > 30 else None
        gap = [(h / b - 1) * 1e4 for _, h, b, _ in s if h and b]
        idx = UNIVERSE.index(coin) if coin in UNIVERSE else None
        basis[coin] = {"universe_index": idx, "hl_mid_sigma_1m_bps": sig1,
                       "mid_basis_bps": dist(gap),
                       "capture_noise_bps": {f"rtt_{r}s": skew_noise_bps(sig1, idx, r)
                                             for r in rtts} if sig1 and idx is not None else None}

    # ---- 5. funding on marked notional ----------------------------------
    rates = defaultdict(list)
    for r in read_jsonl(source / "data" / "funding.jsonl"):
        rates[r["coin"]].append((int(r["ts_settle"]), float(r["rate"])))
    fund = []
    for e in exits:
        f = by_ep_fill[e["ep_id"]]
        s = series.get(e["coin"], [])
        ts_list = [x[0] for x in s]

        def mark_at(dt, s=s, ts_list=ts_list):
            k = bisect_right(ts_list, dt.timestamp()) - 1
            while k >= 0 and not s[k][3]:
                k -= 1
            if k < 0:
                raise ValueError("no mark before settlement")
            return s[k][3]
        fs = FundingSeries.from_pairs(rates[e["coin"]])
        o = datetime.fromtimestamp(f["fill_ts"], UTC)
        c = datetime.fromtimestamp(e["exit_ts"], UTC)
        fixed, n = funding_cost(fs, "short", o, c, notional=NOTIONAL)
        marked, _ = funding_cost(fs, "short", o, c, base_qty=NOTIONAL / f["perp_entry"], price_at=mark_at)
        fund.append({"ep_id": e["ep_id"], "coin": e["coin"], "settlements": n,
                     "replay_funding_received": e["funding_received"],
                     "fixed_notional_received": -fixed, "marked_notional_received": -marked})
    fund_total = {k: sum(x[k] for x in fund) for k in
                  ("replay_funding_received", "fixed_notional_received", "marked_notional_received")}

    # unconditional 1-minute Binance mid change for the same coins (baseline
    # for the conditional drift around fills)
    uncond = []
    for coin in traded:
        s_ = [(t, b) for t, _, b, _ in series.get(coin, []) if b]
        uncond += [(b2 / b1 - 1) * 1e4 for (t1, b1), (t2, b2) in zip(s_, s_[1:]) if 0 < t2 - t1 <= 61]

    # ---- 6b. causal funding availability --------------------------------
    failures = funding_fetch_failures((source / "monitor.log").read_text())
    acted = [(e["event"], e["coin"], e["ts"] / 1000) for e in all_events if e["event"] in ("signal", "exit")]
    funding_availability = {
        "failed_coin_hour_fetches": len(failures),
        "coin_hours_observed": len({(int(d // 3600 * 3600)) for d in decisions}) * len(UNIVERSE),
        "signals_or_exits_in_a_failed_hour": sorted(
            {f"{ev} {c} {datetime.fromtimestamp(t, UTC).isoformat()}" for ev, c, t in acted
             if (int(t // 3600 * 3600), c) in failures}),
        "note": "A failed fetch is backfilled next hour, so the live tracker lacked that "
                "settlement at that decision while the replay (complete funding.jsonl) had it."}

    # ---- 7. forward public depth ----------------------------------------
    forward_depth = None
    if depth_table:
        t = json.loads(Path(depth_table).read_text())["coins"]
        extra, missing_coins = 0.0, set()
        for e in exits:
            row = t.get(e["coin"])
            if not row:
                missing_coins.add(e["coin"])
                continue
            for key in ("bn_buy_1000_beyond_touch_bps", "hl_buy_1000_beyond_touch_bps",
                        "bn_sell_1000_beyond_touch_bps"):
                v = row.get(key)
                if v:
                    extra += NOTIONAL * v["median"] / 1e4
        forward_depth = {"replay_extra_usd_at_sampled_median_impact": extra,
                         "net_usd": net - extra, "annualized_matched_pct": annualized(net - extra, hours),
                         "coins_without_sample": sorted(missing_coins)}

    return {
        "inputs": {"replay_summary": {k: summary[k] for k in ("start", "end", "decision_count",
                                                              "closed_episodes", "net_model_usd",
                                                              "completed_position_hours")},
                   "decisions_in_log": len(decisions), "traded_coins": traded,
                   "forward_fills": sum(e["event"] == "fill" for e in fwd)},
        "break_even": break_even(net, n_ep, hours),
        "fee_scenarios": fee_scen,
        "taker_legs_recorded": taker,
        "replay_extra_usd_recorded_impact_lower_bound": replay_extra_usd,
        "maker_entry": {
            "trade_through_bps": dist([x.get("trade_through_bps") for x in legs if x["event"] == "entry"]),
            "qty_over_displayed_queue": dist([x.get("hl_maker_qty_over_queue") for x in legs if x["event"] == "entry"]),
            "bn_mid_move_next_minute_bps": dist([x.get("bn_mid_next_minute_bps") for x in legs if x["event"] == "entry"]),
            "bn_mid_drift_bps_by_lag_s": {lag: dist([x["bn_mid_drift_bps"].get(lag) for x in legs
                                                     if x["event"] == "entry"])
                                          for lag in (-120, -60, 60, 120, 300)},
            "unconditional_bn_mid_1m_change_bps": dist(uncond),
            "fill_delay_h": dist([x.get("fill_delay_h") for x in legs if x["event"] == "entry"])},
        "decision_conditions": decision_conditions,
        "hl_quote_missing_at_decision": missing,
        "basis": basis,
        "funding_notional": {"episodes": fund, "totals": fund_total},
        "funding_availability": funding_availability,
        "forward_public_depth": forward_depth,
        "legs": legs,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("replay")
    ap.add_argument("forward", nargs="?")
    ap.add_argument("--depth-table")
    a = ap.parse_args()
    print(json.dumps(analyse(a.source, a.replay, a.forward, a.depth_table), indent=2, default=str))
