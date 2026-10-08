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

## Execution, sizing and cost contract

V0 uses market execution for research. Passive limit execution is deliberately not granted without queue-position modeling.

- stake: 100 USDT per signal
- leverage: 1x
- maximum concurrent positions: 5
- both long and short enabled
- market entry + market exit
- explicit fee: 0.0005 (5 bps) per side, 10 bps round-trip
- additional round-trip friction stress: 0 / 5 / 10 / 20 / 40 bps above the explicit fee
- no signal-strength sizing
- no intraday "house money" sizing
- no scale-in
- no partial exits in Phase A

The fee is fixed in the manifest and injected into generated Freqtrade configs by the top-level Quant Finance runner. This prevents a historical decision run from changing later merely because exchange/CCXT default fee metadata changed. The 5 bps-per-side baseline intentionally assumes a regular-user market/taker fee; account-specific VIP discounts are not credited to V0.

## Why every V0 has the same exit

Phase A asks whether the **entry contains information**. If entry logic, target, partials, break-even, trailing and profile exits are optimized together, a profitable result cannot identify which component carries the edge.

Therefore every V0 is already a complete automated Phase-A strategy — entry, sizing, stop and exit are all deterministic — but the exit is intentionally common: fixed 60 minutes plus a catastrophe stop. This is not a claim that 60 minutes is the economically optimal exit.

A surviving signal may enter **Phase B**, where structural invalidation, POC/value-area targets, LVN targets, break-even, partial exits or flow-reversal exits are tested as separately preregistered experiments. They do not get appended to a winning Phase-A backtest for free.

## Quant Finance automation

Canonical command:

```bash
cd ft_userdata
python -m engine.quant_candidate_pipeline \
  --family orderflow_auction_v0 \
  --timerange YYYYMMDD-YYYYMMDD \
  --mode rigorous
```

The pipeline executes, in order:

1. Load the immutable family manifest and require a closed explicit timerange.
2. Apply the explicit fee contract to generated research configs.
3. Register the candidate **in memory as `candidate`**, never `active`.
4. Freeze/hash candidate code, manifest, pairlist, timerange, cost assumptions and Freqtrade image.
5. Download/validate raw public trades and 1m/5m OHLCV for every frozen Binance futures pair.
6. Enable Freqtrade public-trades/orderflow processing in the generated research config.
7. Run `lookahead-analysis`.
8. Run `recursive-analysis` when compatible with the beta orderflow path.
9. Run the existing Engine-v2 viability backtest and export trades.
10. Run chronological fixed-parameter windows; **no WFO/hyperopt in V0**.
11. Run Monte Carlo trade-sequence / missed-entry robustness.
12. Stress additional execution friction and compare with the matched randomized null.
13. Derive a deterministic decision report from the exported trades: per-asset P&L/counts, long/short split, exit reasons, trade duration, gross entry+exit notional turnover, time-in-market and concentration diagnostics.
14. Persist the result artifact with `promotion_authority=false`.
15. Require untouched asset/regime holdout and portfolio admission before forward capital.

A candidate may be executed explicitly by the research runner while the default scheduler/runtime continues to select only `status=active` incumbents.

## Search-budget rule

The V0 family has exactly four signal trials. The values `24`, `96` and `1.5` are hypotheses, not optimums. A new timeframe, threshold, channel horizon, session gate or profile feature is a new trial and cannot silently reuse the same decision holdout.

The family policy applies Bonferroni control across the four planned signal trials (`0.05 / 4 = 0.0125`) to the matched-random-null screen. This is deliberately simple and auditable; it does not claim to solve every form of model-selection bias.

## Required decision report

At minimum: total trades; per-asset trades and P&L; long/short split; net result after declared fees; PF; Sharpe; Sortino; max drawdown; turnover; time-in-market; worst asset; concentration; exit-reason/duration diagnostics; chronological windows; Monte Carlo results; matched-random-null percentile/p-value; and additional friction break-even.

Breadth and concentration are **reported, not auto-gated in V0**. A future threshold for minimum asset breadth or maximum P&L concentration must itself be preregistered instead of being invented after seeing this family’s results.

## Promotion boundary

Passing research does not alter `bots_config.json`, start a service, allocate capital or authorize production. Promotion remains explicit:

`research -> untouched holdout / portfolio admission -> immutable forward/test lane -> Probe -> human decision`
