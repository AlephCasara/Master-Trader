#!/usr/bin/env python3
"""Score the FundingFade forward test (#109) from a freqtrade backtest export.

usage: compare.py <backtest-result.zip> <start YYYY-MM-DD> [live_trades.json]

Only trades OPENED on/after <start> count. Prints per-arm metrics, the
paired bootstrap of daily closed P&L (challenger - control, 7-day blocks)
and the pre-declared decision status. With live_trades.json (from the live
DB, read-only) it also reports how many live entries the control replays.
"""
import json
import random
import sys
import zipfile
from datetime import date, datetime, timedelta

CONTROL, CHALLENGER = "FFF_V2", "FFF_V2_noC"
MIN_CONTROL_TRADES = 30
P_THRESHOLD = 0.80
DD_RATIO = 1.25
BLOCK_DAYS = 7
RESAMPLES = 5000


def load(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.endswith(".json"):
                continue
            data = json.loads(z.read(name))
            if isinstance(data, dict) and CONTROL in data.get("strategy", {}):
                return data["strategy"]
    raise SystemExit(f"no backtest result with {CONTROL} in {zip_path}")


def _day(ts):
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()


def metrics(trades):
    pnl = [t["profit_abs"] for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    equity = peak = dd = 0.0
    for t in sorted(trades, key=lambda t: t["close_date"]):
        equity += t["profit_abs"]
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return {"n": len(pnl), "pnl": round(sum(pnl), 4),
            "win_rate": round(len(wins) / len(pnl), 4) if pnl else None,
            "pf": round(sum(wins) / -sum(losses), 4) if losses and sum(losses) < 0 else None,
            "max_dd": round(dd, 4)}


def daily(trades, start, end):
    days = [start + timedelta(d) for d in range((end - start).days + 1)]
    out = {d: 0.0 for d in days}
    for t in trades:
        d = _day(t["close_date"])
        if d in out:
            out[d] += t["profit_abs"]
    return [out[d] for d in days]


def bootstrap(diff, seed=109):
    blocks = [diff[i:i + BLOCK_DAYS] for i in range(0, len(diff), BLOCK_DAYS)]
    if not blocks:
        return None
    rng = random.Random(seed)
    hits = sum(sum(sum(rng.choice(blocks)) for _ in blocks) > 0 for _ in range(RESAMPLES))
    return round(hits / RESAMPLES, 4)


def main(zip_path, start_s, live_path=None):
    start = date.fromisoformat(start_s)
    strategies = load(zip_path)
    arms = {}
    for name in (CONTROL, CHALLENGER):
        arms[name] = [t for t in strategies[name]["trades"] if _day(t["open_date"]) >= start]
    closes = [_day(t["close_date"]) for a in arms.values() for t in a]
    end = max(closes) if closes else start
    m = {k: metrics(v) for k, v in arms.items()}
    diff = [b - a for a, b in zip(daily(arms[CONTROL], start, end), daily(arms[CHALLENGER], start, end))]
    p = bootstrap(diff)
    delta = round(m[CHALLENGER]["pnl"] - m[CONTROL]["pnl"], 4)
    enough = m[CONTROL]["n"] >= MIN_CONTROL_TRADES
    dd_ok = m[CHALLENGER]["max_dd"] <= DD_RATIO * max(m[CONTROL]["max_dd"], 1e-9)
    switch = enough and delta > 0 and p is not None and p >= P_THRESHOLD and dd_ok
    result = {"window": [start.isoformat(), end.isoformat()], "arms": m,
              "delta_pnl": delta, "p_challenger_better": p,
              "control_trades_needed": MIN_CONTROL_TRADES, "dd_ok": dd_ok,
              "decision": ("switch_to_V2_noC" if switch else
                           "keep_V2" if enough else "insufficient_trades")}
    if live_path:
        live = json.load(open(live_path))
        def key(t):
            opened = datetime.fromisoformat(str(t["open_date"]).replace("Z", "+00:00"))
            return t["pair"], opened.date(), opened.hour
        replayed = {key(t) for t in arms[CONTROL]}
        live = [t for t in live if _day(t["open_date"]) >= start]
        result["live_calibration"] = {"live_entries": len(live),
                                      "replayed_by_control": sum(key(t) in replayed for t in live)}
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    main(*sys.argv[1:])
