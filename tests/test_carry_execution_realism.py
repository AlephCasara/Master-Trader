"""Carry execution-realism audit helpers (issue #27)."""
import gzip
import importlib.util
import json
import math
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "carry_exec", Path(__file__).parents[1] / "research/hl_carry_shadow/execution_realism.py")
er = importlib.util.module_from_spec(spec)
spec.loader.exec_module(er)


def test_break_even_matches_the_published_cost_stress():
    # Replay: $27.64 over 9 episodes, 1,754.35 position-hours. The published
    # stress says +5 bps/fill -> $9.64 and +10 bps/fill -> -$8.36, so zero
    # sits at 27.64 / (36 * $1000) = 7.68 bps per fill.
    be = er.break_even(27.63791333777202, 9, 1754.35)
    assert be["0%"]["extra_bps_per_fill"] == pytest.approx(7.677, abs=1e-3)
    assert 27.64 - 36 * 1000 * 5 / 1e4 == pytest.approx(9.64)
    # The 9% hurdle leaves almost nothing; the original 12% is already missed.
    assert be["9%"]["required_net_usd"] == pytest.approx(0.09 * 1754.35 * 1500 / 8760)
    assert 0 < be["9%"]["extra_bps_per_fill"] < 0.2
    assert be["12%"]["extra_bps_per_fill"] < 0
    assert er.annualized(27.63791333777202, 1754.35) == pytest.approx(9.2003, abs=1e-3)


def test_quotes_map_only_to_decisions_at_or_after_them_within_120s():
    decisions = [1000.0, 4600.0]
    assert er.decision_for_quote(decisions, 960) == 0
    assert er.decision_for_quote(decisions, 1000) == 0
    assert er.decision_for_quote(decisions, 1001) is None   # after decision 0, >120 s before 1
    assert er.decision_for_quote(decisions, 870) is None    # 130 s stale at decision 0
    assert er.decision_for_quote(decisions, 4500) == 1


def test_leg_metrics_bounds_impact_when_recorded_depth_is_short():
    row = {"hl_bid": 0.999, "hl_ask": 1.0, "hl_ask_sz": 400.0, "hl_ask2": [1.001, 300.0],
           "bn_bid": 0.998, "bn_bid_sz": 50.0, "bn_ask": 1.0, "bn_ask_sz": 2000.0}
    m = er.leg_metrics(row, 1000.0)
    assert m["hl_spread_bps"] == pytest.approx(0.001 / 0.9995 * 1e4)
    assert m["bn_buy"]["l1_coverage"] == 2.0 and m["bn_buy"]["impact_bps"] == 0.0
    assert not m["bn_buy"]["impact_is_lower_bound"]
    hl = m["hl_buy"]
    assert hl["recorded_levels"] == 2 and hl["recorded_coverage"] == pytest.approx(0.7)
    assert hl["impact_is_lower_bound"]
    # 400 @1.000 + 600 @1.001 (last level extended) -> avg 1.0006 -> 6 bps
    assert hl["impact_bps"] == pytest.approx(6.0, abs=0.01)
    # Binance records one level, so a short bid side is a zero-impact lower bound
    assert m["bn_sell"]["l1_coverage"] == 0.05 and m["bn_sell"]["impact_bps"] == pytest.approx(0.0)
    assert m["bn_sell"]["impact_is_lower_bound"]


def test_missing_quotes_do_not_fabricate_metrics():
    m = er.leg_metrics({"hl_bid": None, "hl_ask": None, "bn_bid": 1.0, "bn_ask": 1.0001,
                        "bn_bid_sz": 5000, "bn_ask_sz": 5000}, 100.0)
    assert m["hl_spread_bps"] is None and m["hl_buy"] is None
    assert m["bn_buy"]["l1_coverage"] == 50.0


def test_capture_noise_grows_with_poll_position():
    first = er.skew_noise_bps(10.0, 0, 0.2)
    last = er.skew_noise_bps(10.0, 39, 0.2)
    assert first == pytest.approx(10.0 * math.sqrt(0.2 / 60))
    assert last == pytest.approx(10.0 * math.sqrt((39 * 0.35 + 0.2) / 60))
    assert last > 8 * first


def test_hedge_drift_measures_binance_mid_around_the_fill_snapshot():
    series = [(t, None, 100.0 + (t - 1000) / 60 * 0.15, None) for t in range(880, 1400, 60)]
    d = er.hedge_drift(series, 1000)
    assert d[60] == pytest.approx(15.0) and d[-60] == pytest.approx(-15.0)
    assert d[300] == pytest.approx(75.0)
    assert er.hedge_drift(series, 1001) == {}          # no snapshot exactly at the fill
    gappy = [x for x in series if x[0] != 1060]
    assert 60 not in er.hedge_drift(gappy, 1000)        # missing minute is skipped, not interpolated


def test_funding_fetch_failures_are_bucketed_by_hour_and_coin():
    log = ("2026-07-13T08:00:54.199977+00:00 fundingHistory XRP failed: <HTTPError 429: 'Too Many Requests'>\n"
           "2026-07-13T08:00:55.000000+00:00 funding fetch: +39 settlements\n"
           "2026-07-13T11:00:31.978466+00:00 l2Book ZEC failed: <HTTPError 429: 'Too Many Requests'>\n")
    from datetime import datetime, timezone
    hour = int(datetime(2026, 7, 13, 8, tzinfo=timezone.utc).timestamp())
    assert er.funding_fetch_failures(log) == {(hour, "XRP")}


def test_end_to_end_on_a_tiny_synthetic_archive(tmp_path):
    src, rep = tmp_path / "src", tmp_path / "rep"
    (src / "data").mkdir(parents=True)
    (rep / "data").mkdir(parents=True)
    t0 = 1_786_000_000 - (1_786_000_000 % 3600)
    rows = []
    for m in range(0, 3 * 60):
        ts = t0 + 60 * m
        px = 1.0 + 0.0001 * (m % 7)
        rows.append({"ts": ts, "coin": "ZRO", "hl_bid": px - 0.0005, "hl_bid_sz": 5000, "hl_ask": px,
                     "hl_ask_sz": 5000, "hl_bid2": [px - 0.001, 100], "hl_ask2": [px + 0.0005, 100],
                     "hl_mark": px, "bn_bid": px - 0.0004, "bn_bid_sz": 3000, "bn_ask": px + 0.0001,
                     "bn_ask_sz": 3000})
    with gzip.open(src / "data" / "bbo_20260806.jsonl.gz", "wt") as f:
        f.write("\n".join(json.dumps(r) for r in rows))
    (src / "data" / "funding.jsonl").write_text("\n".join(
        json.dumps({"ts_settle": (t0 + 3600 * h) * 1000, "coin": "ZRO", "rate": 1e-4}) for h in range(4)))
    from datetime import datetime, timezone
    log = [f"{datetime.fromtimestamp(t0 + 3600 * h + 48, timezone.utc).isoformat()} tracker: {{}}" for h in range(3)]
    (src / "monitor.log").write_text("\n".join(log) + "\n")
    fill_ts, exit_ts = t0 + 60, t0 + 2 * 3600 + 60
    ev = [{"event": "signal", "ts": (t0 + 48) * 1000, "coin": "ZRO", "ep_id": "ep1", "posted_ask": 1.0},
          {"event": "fill", "ts": (t0 + 3600) * 1000, "coin": "ZRO", "ep_id": "ep1", "fill_ts": fill_ts,
           "fill_delay_h": 0.01, "perp_entry": 1.0, "spot_entry": 1.0002},
          {"event": "exit", "ts": (t0 + 7300) * 1000, "coin": "ZRO", "ep_id": "ep1", "fill_ts": fill_ts,
           "exit_ts": exit_ts, "perp_exit": 1.0001, "spot_exit": 0.9997, "funding_received": 0.2,
           "basis_pnl": 0.0, "total_fees": 2.1, "net": -1.9}]
    (rep / "data" / "episodes.jsonl").write_text("\n".join(json.dumps(e) for e in ev))
    (rep / "summary.json").write_text(json.dumps({
        "start": "s", "end": "e", "decision_count": 3, "closed_episodes": 1, "net_model_usd": -1.9,
        "completed_position_hours": 2.0}))
    out = er.analyse(src, rep)
    assert out["inputs"]["traded_coins"] == ["ZRO"]
    assert out["funding_notional"]["totals"]["fixed_notional_received"] == pytest.approx(0.2)
    assert out["taker_legs_recorded"]["hl_buy"]["n"] == 1
    assert out["fee_scenarios"]["base_tier_fees"]["net_usd"] == pytest.approx(-1.9 - 0.5)
    assert out["decision_conditions"]["all"]["hl_spread_bps"]["n"] == 3
    entry = [x for x in out["legs"] if x["event"] == "entry"][0]
    assert entry["hl_maker_queue_ahead_qty"] == 5000
