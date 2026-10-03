# OITrendPullbackV1: causal OI history and price-only control (issue #18)

Research only. The bot stays dry-run; nothing here changes the deployed strategy,
its 0.0 threshold or any allocation. Preregistration `oi-gate-recalibration-2026-08-30`
is closed with its evidence withdrawn; this work answers its `redo_requirements`.

## Why the backtest had no entries

`OITrendPullbackV1` fills `oi_growth` only in live/dry runs, so in a backtest the
term `oi_growth >= oi_min_growth` is `NaN >= 0.0`, i.e. False, and the strategy
cannot enter. That is deliberate fail-closed behaviour: copying the live reading
onto old candles would invent history.

## Causal OI path (`oi_history.py`)

Binance `/futures/data/openInterestHist`, period 15m, paginated 500 per page. Binance
serves only the most recent ~30 days (an older `startTime` returns HTTP 400), so
`fetch` merges into a CSV archive; re-running it accumulates history.

Join rule, per candle (tests: `tests/test_oi_causal_history.py`):

| | live bot | historical path |
|---|---|---|
| decision time for candle opened at T | candle close, T+1h | T+1h |
| OI endpoint | latest 5-minute poll, at most 15 min old | latest record with `timestamp + lag <= T+1h`, i.e. T+45m |
| baseline | latest poll in [end-50m, end-45m] | record stamped exactly end-45m |
| missing/stale | no entry | no entry (never copied or interpolated) |

The record stamped T+1h is published 35-140 s after T+1h (measured 2026-10-03,
ETH/SOL/XRP), so it is not available at the decision; the default lag is 5 min.
Consequence: the historical window is [T, T+45m] while the live bot's is roughly
[T+10..15m, T+55..60m]. That 10-15 minute offset is the price of causality on a
15-minute grid and is a stated difference, not an equivalence claim. The
withdrawn 2026-08-30 calibration used [T-45m, T] against a decision at T: one
hour early.

Over 2026-09-03..10-02, 8 pairs: 5,736 candles with causal growth, 24 fail
closed (window start), p50 +0.00%, p90 +0.51%, p99 +1.53%, 50.0% >= 0.

## Pre-declared backtest (written before any run)

Variants (`strategies/OITrendCausalResearch.py`, each inherits the deployed
`OITrendPullbackV1.py`, md5 `9f69c2fc1df8adb94c7fe23949d040d4`, unchanged):

1. `OITrendPullbackV1` as deployed: expected 0 trades (confirms the issue).
2. `OITrendPullbackV1CausalOI`: causal OI growth, threshold 0.0 (deployed value, not tuned).
3. `OITrendPullbackV1PriceOnly`: OI term always passes; everything else identical.
4. `OITrendPullbackV1Placebo01..20`: OI term replaced by a seeded coin flip passing 50%
   of candles (the unconditional pass rate of the causal gate).

Setup: Freqtrade 2026.7 (fleet image digest), `bt_config.json` = live config minus
webhook/API/DB, spot, stake 10 USDT, `max_open_trades` 1, strategy protections on,
`--timeframe-detail 1m`, `--fee 0.001` (Binance spot base-tier taker, both sides),
Binance SPOT 1h/1m klines, timerange 2026-09-04 to 2026-09-30 (the overlap of the
OI archive and the existing spot data). Halves: trades of the single full run split by
entry time at 2026-09-17 00:00 UTC.
No parameter is selected, so the halves are a stability check, not IS/OOS tuning.
Slippage sensitivity: subtract 5 bps per fill from the trade list.

Metrics: trades, profit factor, net USDT, win rate, per half.

Reading rule: the OI gate shows discriminative power only if CausalOI beats both
PriceOnly and the 90th percentile of the placebo distribution on profit factor and
net, in both halves. Anything else is "not demonstrated". At roughly 30 days and a
handful of trades no outcome authorizes capital: the closed prereg's >= 25 closed
trades and the Phase-3 bar still apply.
