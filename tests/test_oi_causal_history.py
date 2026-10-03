"""Causal OI history join for OITrendPullbackV1 research (issue #18)."""
import importlib.util
import random
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "oi_history", Path(__file__).parents[1] / "research/oi_trend_causal/oi_history.py")
oh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oh)

STEP, HOUR, MIN = oh.STEP_MS, oh.HOUR_MS, 60_000
T = 1_789_000_000_000 // HOUR * HOUR   # a candle open on the hour


def grid(start, n, value=lambda i: 1000.0 + i):
    return [(start + i * STEP, value(i)) for i in range(n)]


def test_candle_is_decided_at_its_close_not_its_open():
    # The withdrawn 2026-08-30 calibration scored candle T against OI ending at T.
    assert oh.candle_decision_ms(T) == T + HOUR
    recs = grid(T - 2 * HOUR, 20)
    row = oh.align_candles(recs, [T])[0]
    assert row["decision_ms"] == T + HOUR
    # Endpoint is the last record published before the close: T+45m, not T, not T+1h.
    assert row["oi_end_ms"] == T + 45 * MIN


def test_record_stamped_at_the_decision_is_not_yet_published():
    recs = grid(T, 8)                              # T, T+15m, ..., T+1h45
    g, reason, end = oh.causal_oi_growth(recs, T + HOUR, lag_ms=35_000)
    assert reason == "ok" and end == T + 45 * MIN  # T+1h needs 35 s to appear
    assert g == pytest.approx(1003.0 / 1000.0 - 1)
    # Once the lag has elapsed the T+1h record is fair game.
    _, _, end_later = oh.causal_oi_growth(recs, T + HOUR + 35_000, lag_ms=35_000)
    assert end_later == T + HOUR


def test_future_records_can_never_change_a_decision():
    rng = random.Random(7)
    recs = grid(T - 3 * HOUR, 40, value=lambda i: 1000 + rng.random() * 50)
    decision = T + HOUR
    before = oh.causal_oi_growth(recs, decision)
    tampered = [(t, v * (3 if t + oh.DEFAULT_LAG_MS > decision else 1)) for t, v in recs]
    tampered += [(decision + 10 * STEP, 1e9)]
    assert oh.causal_oi_growth(sorted(tampered), decision) == before


def test_positive_and_negative_growth_cases():
    up = [(T, 100.0), (T + 15 * MIN, 101.0), (T + 30 * MIN, 101.5), (T + 45 * MIN, 102.0)]
    g, reason, _ = oh.causal_oi_growth(up, T + HOUR)
    assert reason == "ok" and g == pytest.approx(0.02)
    down = [(t, 200.0 - i) for i, (t, _) in enumerate(up)]
    g, reason, _ = oh.causal_oi_growth(down, T + HOUR)
    assert reason == "ok" and g == pytest.approx(197 / 200 - 1) and g < 0


def test_missing_or_stale_data_fails_closed():
    full = grid(T - HOUR, 12)
    # Baseline record missing: never substitute a neighbour.
    no_base = [r for r in full if r[0] != T]
    assert oh.causal_oi_growth(no_base, T + HOUR)[:2] == (None, "missing_baseline")
    # Endpoint gap: the previous record is now older than 15 min + lag.
    no_end = [r for r in full if r[0] != T + 45 * MIN]
    assert oh.causal_oi_growth(no_end, T + HOUR)[:2] == (None, "stale_endpoint")
    assert oh.causal_oi_growth([], T + HOUR)[:2] == (None, "no_record_available")
    bad = [(t, 0.0 if t == T else v) for t, v in full]
    assert oh.causal_oi_growth(bad, T + HOUR)[:2] == (None, "invalid_value")


def test_pagination_stitches_pages_and_drops_out_of_range_rows():
    calls = []

    def fake_get(url):
        q = dict(p.split("=") for p in url.split("?")[1].split("&"))
        start, end = int(q["startTime"]), int(q["endTime"])
        calls.append((start, end))
        rows = [{"timestamp": t, "sumOpenInterest": str(t / 1e9)} for t in range(start, end + 1, STEP)]
        rows.append({"timestamp": start - STEP, "sumOpenInterest": "1"})  # overlap junk
        return rows

    start, end = T, T + 1200 * STEP
    rows = oh.fetch_oi_hist("ETHUSDT", start, end, get=fake_get, sleep_s=0)
    assert len(rows) == 1201 and rows[0][0] == start and rows[-1][0] == end
    assert len(calls) == 3 and all(e - s <= (oh.PAGE - 1) * STEP for s, e in calls)
    assert len({t for t, _ in rows}) == len(rows)


def test_archive_merge_keeps_first_value_and_accumulates(tmp_path):
    path = tmp_path / "oi.csv"
    assert oh.merge_archive(path, "ETHUSDT", [(T, 1.0), (T + STEP, 2.0)]) == 2
    assert oh.merge_archive(path, "ETHUSDT", [(T + STEP, 99.0), (T + 2 * STEP, 3.0)]) == 1
    assert oh.read_archive(path)["ETHUSDT"] == [(T, 1.0), (T + STEP, 2.0), (T + 2 * STEP, 3.0)]


def _summarizer():
    s = importlib.util.spec_from_file_location(
        "oi_summary", Path(__file__).parents[1] / "research/oi_trend_causal/summarize_backtest.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def test_backtest_summary_metrics_halves_and_reading_rule():
    sm = _summarizer()

    def t(day, pnl):
        return {"open_date": f"2026-09-{day:02d} 05:00:00+00:00", "profit_abs": pnl,
                "stake_amount": 10.0, "exit_reason": "roi" if pnl > 0 else "stop_loss"}
    result = {"strategy": {
        "OITrendPullbackV1CausalOI": {"trades": [t(5, 0.3), t(6, -0.1), t(20, 0.2), t(21, -0.1)]},
        "OITrendPullbackV1PriceOnly": {"trades": [t(5, 0.1), t(6, -0.2), t(20, 0.1), t(21, -0.2)]},
        **{f"OITrendPullbackV1Placebo{i:02d}": {"trades": [t(5, 0.05 * i / 20), t(6, -0.2), t(20, -0.1),
                                                           t(21, 0.01)]}
           for i in range(1, 21)}}}
    s = sm.summarize(result)
    g = s["OITrendPullbackV1CausalOI"]
    assert g["full"] == {"trades": 4, "net_usdt": 0.3, "profit_factor": 2.5, "win_rate": 0.5}
    assert g["half1"]["trades"] == 2 and g["half2"]["trades"] == 2
    assert g["full_slip"]["net_usdt"] == pytest.approx(0.3 - 4 * 2 * 5e-4 * 10)
    rule = sm.reading_rule(s)
    assert rule["by_part"]["full"]["net_usdt"]["beats_control_and_p90"]
    # second half: gate PF 2.0 vs placebo PF 0.1 and control 0.5, net +0.1 > -0.09 and -0.1
    assert rule["by_part"]["half2"]["profit_factor"]["beats_control_and_p90"]
    assert rule["discriminative_power"] == "demonstrated"
