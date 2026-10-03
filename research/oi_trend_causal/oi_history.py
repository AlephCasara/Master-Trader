#!/usr/bin/env python3
"""Causal historical open-interest path for OITrendPullbackV1 (issue #18).

The live bot polls /fapi/v1/openInterest every 5 minutes and, at each 1h
decision, uses growth = OI(latest observation) / OI(observation 45 minutes
earlier) - 1, failing closed when the reading is missing or older than 15
minutes. In backtests it has no OI at all, so the conjunction never fires.

History comes from Binance /futures/data/openInterestHist (period 15m, at most
500 records per page, and only the most recent ~30 days are served). Each
record's ``timestamp`` is the snapshot time; the record becomes visible some
time later (35-140 s observed on 2026-10-03). This module joins it to candles
CAUSALLY:

* A candle opening at T is decided at T + 1h, when it closes. That is the
  only moment the live bot can act on it (the 2026-08-30 calibration scored
  it against OI ending at T and was withdrawn for exactly this).
* At decision D the usable record is the latest one with
  ``timestamp + availability_lag <= D``. With any lag in (0, 15 min), the
  record stamped exactly D is NOT usable; the endpoint is D - 15 min.
* The baseline is the record stamped exactly ``endpoint - 45 min`` (the live
  rule accepts at most one 5-minute poll interval before the 45-minute mark,
  and on a 15-minute grid only that record qualifies).
* Missing endpoint, missing baseline, a non-positive value, or an endpoint
  older than ``max_age`` fails closed (None), never a copied or interpolated
  value. On a 15-minute grid the freshest causal record is 15 min + lag old,
  so ``max_age`` defaults to that, not to the live bot's 15 minutes.

Usage:
    python3 oi_history.py fetch ARCHIVE.csv ETH SOL ...      # merge into archive
    python3 oi_history.py align ARCHIVE.csv OUT_DIR --start 2026-09-03 --end 2026-10-01
    python3 oi_history.py distribution OUT_DIR
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
import urllib.parse
import urllib.request
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

URL = "https://fapi.binance.com/futures/data/openInterestHist"
STEP_MS = 15 * 60 * 1000
HOUR_MS = 60 * 60 * 1000
LOOKBACK_MS = 45 * 60 * 1000          # OITrendPullbackV1.oi_lookback_s
DEFAULT_LAG_MS = 5 * 60 * 1000        # conservative vs the observed 35-140 s
PAGE = 500
SERVED_DAYS = 30


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "mt-research/1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def fetch_oi_hist(symbol: str, start_ms: int, end_ms: int, get=_get_json,
                  sleep_s: float = 0.25) -> list[tuple[int, float]]:
    """All 15m records with start_ms <= timestamp <= end_ms, paginated
    forward 500 at a time, deduplicated and sorted. Binance serves only the
    last ~30 days; earlier start times return an error or nothing."""
    out: dict[int, float] = {}
    cursor = start_ms
    while cursor <= end_ms:
        q = urllib.parse.urlencode({"symbol": symbol, "period": "15m", "limit": PAGE,
                                    "startTime": cursor,
                                    "endTime": min(cursor + (PAGE - 1) * STEP_MS, end_ms)})
        rows = get(f"{URL}?{q}")
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected response for {symbol}: {str(rows)[:200]}")
        for r in rows:
            ts = int(r["timestamp"])
            if start_ms <= ts <= end_ms:
                out[ts] = float(r["sumOpenInterest"])
        cursor = cursor + PAGE * STEP_MS
        if sleep_s:
            time.sleep(sleep_s)
    return sorted(out.items())


def read_archive(path: Path) -> dict[str, list[tuple[int, float]]]:
    data: dict[str, dict[int, float]] = {}
    if Path(path).exists():
        with open(path) as f:
            for r in csv.DictReader(f):
                data.setdefault(r["symbol"], {})[int(r["timestamp_ms"])] = float(r["sum_open_interest"])
    return {s: sorted(v.items()) for s, v in data.items()}


def merge_archive(path: Path, symbol: str, rows: list[tuple[int, float]]) -> int:
    """Merge records into a CSV archive (re-running accumulates history beyond
    the 30 days Binance serves). Existing timestamps are kept unchanged."""
    data = read_archive(path)
    have = dict(data.get(symbol, []))
    added = 0
    for ts, oi in rows:
        if ts not in have:
            have[ts] = oi
            added += 1
    data[symbol] = sorted(have.items())
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "timestamp_ms", "sum_open_interest"])
        for s in sorted(data):
            for ts, oi in data[s]:
                w.writerow([s, ts, repr(oi)])
    tmp.replace(path)
    return added


def causal_oi_growth(records: list[tuple[int, float]], decision_ms: int, *,
                     lag_ms: int = DEFAULT_LAG_MS, lookback_ms: int = LOOKBACK_MS,
                     max_age_ms: int | None = None) -> tuple[float | None, str, int | None]:
    """(growth, reason, endpoint_ms) usable at decision_ms. ``records`` must be
    sorted (timestamp_ms, oi). growth is None whenever it would not be known."""
    if max_age_ms is None:
        max_age_ms = STEP_MS + lag_ms
    times = [t for t, _ in records]
    k = bisect_right(times, decision_ms - lag_ms) - 1
    if k < 0:
        return None, "no_record_available", None
    end_ts, end_oi = records[k]
    if decision_ms - end_ts > max_age_ms:
        return None, "stale_endpoint", end_ts
    j = bisect_right(times, end_ts - lookback_ms) - 1
    if j < 0 or times[j] != end_ts - lookback_ms:
        return None, "missing_baseline", end_ts
    base_oi = records[j][1]
    if not (end_oi > 0 and base_oi > 0 and math.isfinite(end_oi) and math.isfinite(base_oi)):
        return None, "invalid_value", end_ts
    return end_oi / base_oi - 1.0, "ok", end_ts


def candle_decision_ms(candle_open_ms: int, timeframe_ms: int = HOUR_MS) -> int:
    """A Freqtrade candle dated T (its open) is acted on when it closes."""
    return candle_open_ms + timeframe_ms


def align_candles(records, candle_opens_ms, **kw) -> list[dict]:
    rows = []
    for t in candle_opens_ms:
        d = candle_decision_ms(t)
        g, reason, end_ts = causal_oi_growth(records, d, **kw)
        rows.append({"date_ms": t, "decision_ms": d, "oi_end_ms": end_ts,
                     "oi_growth": g, "reason": reason})
    return rows


def write_aligned(out_dir: Path, symbol: str, rows: list[dict]) -> Path:
    """One CSV per symbol, read by the research strategy by candle date."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol}-oi_growth_causal.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date_ms", "decision_ms", "oi_end_ms", "oi_growth", "reason"])
        w.writeheader()
        for r in rows:
            w.writerow({**r, "oi_growth": "" if r["oi_growth"] is None else repr(r["oi_growth"])})
    return path


def _ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("archive")
    f.add_argument("coins", nargs="+")
    f.add_argument("--days", type=float, default=SERVED_DAYS - 0.1)
    a = sub.add_parser("align")
    a.add_argument("archive")
    a.add_argument("out_dir")
    a.add_argument("--start", required=True)
    a.add_argument("--end", required=True)
    a.add_argument("--lag-s", type=int, default=DEFAULT_LAG_MS // 1000)
    d = sub.add_parser("distribution")
    d.add_argument("out_dir")
    args = ap.parse_args()

    if args.cmd == "fetch":
        now = int(time.time() * 1000)
        start = (now - int(args.days * 86_400_000)) // STEP_MS * STEP_MS + STEP_MS
        for coin in args.coins:
            sym = coin.upper() + "USDT"
            rows = fetch_oi_hist(sym, start, now)
            added = merge_archive(Path(args.archive), sym, rows)
            first = datetime.fromtimestamp(rows[0][0] / 1000, timezone.utc) if rows else None
            last = datetime.fromtimestamp(rows[-1][0] / 1000, timezone.utc) if rows else None
            expected = (rows[-1][0] - rows[0][0]) // STEP_MS + 1 if rows else 0
            print(f"{sym}: {len(rows)} records ({expected - len(rows)} missing on the grid) "
                  f"{first} .. {last}; +{added} new in archive")
    elif args.cmd == "align":
        archive = read_archive(Path(args.archive))
        opens = list(range(_ms(args.start), _ms(args.end), HOUR_MS))
        for sym, recs in sorted(archive.items()):
            rows = align_candles(recs, opens, lag_ms=args.lag_s * 1000)
            path = write_aligned(Path(args.out_dir), sym, rows)
            ok = sum(r["reason"] == "ok" for r in rows)
            print(f"{sym}: {ok}/{len(rows)} candles with causal OI growth -> {path}")
    else:
        vals, reasons = [], {}
        for path in sorted(Path(args.out_dir).glob("*-oi_growth_causal.csv")):
            with open(path) as fh:
                for r in csv.DictReader(fh):
                    reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
                    if r["oi_growth"]:
                        vals.append(float(r["oi_growth"]))
        vals.sort()

        def q(p):
            return vals[min(len(vals) - 1, int(p * (len(vals) - 1)))] * 100 if vals else None
        print(json.dumps({"n": len(vals), "reasons": reasons, "p10_pct": q(.1), "p50_pct": q(.5),
                          "p90_pct": q(.9), "p99_pct": q(.99),
                          "share_ge_0": sum(v >= 0 for v in vals) / len(vals) if vals else None},
                         indent=2))


if __name__ == "__main__":
    main()
