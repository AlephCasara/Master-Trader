# Order-flow auction strategies — Quant Finance contract

Status: **research only**. This document defines how the four V0 strategies enter, exit, select assets and pass through Quant Finance. It authorizes no live capital.

## Economic question

The source live stream is reduced to observable claims only:

`market state -> location -> public-trade participation -> confirmation -> invalidation/exit`

The V0 family does not encode participant identity, alleged trapped traders, fixed NQ contract thresholds, universal London/New York behavior, or claims that a low-volume node must fill.

## Automated strategies

### AuctionExpansionV0
- **Long entry:** first completed 5m close above the prior 24 completed-bar high AND reported public-trade flow z-score >= +1.5.
- **Short entry:** symmetric below the prior 24-bar low with flow z-score <= -1.5.
- **Exit:** automatic 60-minute time exit, or -5% catastrophe stop.

### RetestContinuationV0
- **Long entry:** prior candle closed above its old 24-bar channel; current candle retests that original boundary, closes back above it, and flow z-score remains >= +1.5.
- **Short entry:** symmetric retest/rejection below the original lower boundary with flow z-score <= -1.5.
- **Exit:** automatic 60-minute time exit, or -5% catastrophe stop.

### FailedAuctionReversionV0
- **Long entry:** current candle trades below the prior 24-bar low and closes back inside the channel.
- **Short entry:** trades above the prior 24-bar high and closes back inside.
- Raw public trades are still mandatory so its sample is directly comparable with AbsorptionReversalV0.
- **Exit:** automatic 60-minute time exit, or -5% catastrophe stop.

### AbsorptionReversalV0
- **Long entry:** FailedAuctionReversionV0 long condition plus extreme reported sell-side flow (`flow_z <= -1.5`) that nevertheless failed to hold below the channel.
- **Short entry:** FailedAuctionReversionV0 short condition plus extreme reported buy-side flow (`flow_z >= +1.5`) that failed to hold above the channel.
- **Exit:** automatic 60-minute time exit, or -5% catastrophe stop.

The last pair is an explicit ablation: `FailedAuctionReversionV0` asks whether the price structure has information; `AbsorptionReversalV0` asks whether public-trade flow adds information beyond it.

## Flow definition and causality

The strategy reads Freqtrade's raw `dataframe["trades"]` for each completed candle. It sums the reported `amount` by trade `side` and computes:

`flow_imbalance = (buy_side_volume - sell_side_volume) / total_reported_volume`

The current candle is standardized against mean/std from the **previous 96 completed candles**. No OHLCV candle volume is substituted for raw trades.

The 24-bar price channel is also built from completed prior bars. A signal produced by candle `t` is therefore actionable only after candle `t` closes.

## Asset contract

The first development universe is frozen in `orderflow_auction_v0.json`. It is an explicit list, not a dynamic top-volume list evaluated using today's market.

The list is **development data**, not an untouched asset holdout. Any candidate that survives development must be frozen before a separately selected exact-set holdout is inspected.

## Execution and sizing contract

V0 uses market execution for research. Freqtrade backtesting assumes fills and therefore subsequent friction stress is mandatory; passive limit execution is deliberately not granted without queue-position modeling.

- stake: 100 USDT per signal
- leverage: 1x
- maximum concurrent positions: 5
- both long and short enabled
- no signal-strength sizing
- no intraday "house money" sizing
- no scale-in
- no partial exits in Phase A

## Why every V0 has the same exit

Phase A asks whether the entry contains information. If entry logic, target, partials, break-even, trailing and profile exits are optimized together, a profitable result cannot identify which component carries the edge.

Therefore every V0 uses a fixed 60-minute exit plus a catastrophe stop. Phase B exists only for a surviving signal and becomes a new recorded experiment.

## Quant Finance automation

The family runner must execute, in order:

1. Load the immutable family manifest and require a closed explicit timerange.
2. Register the candidate **in memory as `candidate`**, never `active`.
3. Freeze/hash candidate code, manifest, pairlist, timerange, cost assumptions and Freqtrade image.
4. Download/validate raw public trades and 1m/5m OHLCV for every frozen Binance futures pair.
5. Enable Freqtrade public-trades/orderflow processing in the generated research config.
6. Run `lookahead-analysis`.
7. Run `recursive-analysis` when compatible with the beta orderflow path.
8. Run the existing Engine-v2 viability backtest and export trades.
9. Run chronological fixed-parameter windows; **no WFO/hyperopt in V0**.
10. Run Monte Carlo trade-sequence / missed-entry robustness.
11. Emit per-asset, long/short and chronological-window results.
12. Require later cost stress, matched-random null, untouched asset/regime holdout and portfolio admission before forward capital.

A candidate may be executed explicitly by the research runner while the default live/research scheduler continues to select only `status=active` incumbents.

## Search-budget rule

The V0 family has exactly four signal trials. The values `24`, `96` and `1.5` are hypotheses, not optimums. A new timeframe, threshold, channel horizon, session gate or profile feature is a new trial and cannot silently reuse the same decision holdout.

## Required decision report

At minimum: total trades; per-asset trades and P&L; long/short split; net result after declared fees; PF; Sharpe; Sortino; max drawdown; turnover; time-in-market; worst asset; chronological windows; Monte Carlo results; matched-random-null percentile; and additional friction break-even.

## Promotion boundary

Passing research does not alter `bots_config.json`, start a service, allocate capital or authorize production. Promotion remains explicit: research -> immutable forward/test lane -> Probe -> human decision.
