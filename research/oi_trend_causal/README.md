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

The record stamped T+1h is published 35-157 s after T+1h (9 records, ETH/SOL/XRP,
polled every ~13 s on 2026-10-03 00:00-00:50 UTC: SOL/XRP 35-49 s, ETH 142-157 s), so
it is not available at the decision; the default lag is 5 min. Historical values sat
within 0.01-0.08% of the live endpoint read seconds apart (a near-constant offset per
symbol, which cancels in a growth ratio).
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

## Results (run 2026-10-03 00:25 UTC, after the pre-declaration was committed)

Inputs and checksums: `results/2026-10-03-input-sha256.txt`; per-variant metrics and the
reading rule: `results/2026-10-03-backtest-summary.json` (`summarize_backtest.py`). The OI
archive and Freqtrade export stay on the VPS in `research/oi_trend_causal_2026-10-02/`.

| variant | trades | PF | net USDT | net, +5 bps/fill | win | half 1 (n, net) | half 2 (n, net) |
|---|---:|---:|---:|---:|---:|---|---|
| deployed `OITrendPullbackV1` | 0 | - | 0 | - | - | - | - |
| CausalOI | 13 | 1.24 | +0.27 | +0.14 | 38% | 5, +0.77 | 8, -0.50 |
| PriceOnly | 18 | 1.88 | +0.94 | +0.77 | 50% | 6, +0.71 | 12, +0.23 |
| Placebo x20, median | 12 | 1.73 | +0.53 | +0.43 | - | - | - |

- The deployed strategy takes **0 trades** in backtest, confirming the issue.
- CausalOI ranks at the **30th percentile** of the placebo distribution on both PF and net
  (65-70th in half 1, 10th in half 2) and beats neither PriceOnly nor the placebo p90 in
  either half. Reading rule: **discriminative power not demonstrated**.
- Join check on the actual trades: all 13 CausalOI entries had growth >= 0 from a record
  15 minutes old at the decision; all 9 PriceOnly trades the gate skipped had negative growth.
- Over this window the archive had no missing 15m records (the 2026-08-30 note's 28% NaN
  does not recur), so fail-closed exclusions are only the first three candles per pair.

Stakes are 10 USDT; 13-18 trades over 26 days in one regime carry no inferential weight,
and nothing here is evidence that the price-only variant has an edge either. The result
agrees with the withdrawn redo (gate at the 57th percentile of its null).

Limitations: 30 days of history only (Binance serves no more; a longer test needs `fetch`
run on a schedule, which is not set up here). Backtest entries fill at the next candle open,
not the live limit-at-bid with a 10-minute unfilled timeout. Slippage is a post-hoc
sensitivity. The historical OI window sits 10-15 minutes earlier than the live one.
