# FundingFade forward test: V2 vs V2 without protections (#109)

Preregistration: `ff-forward-v2-vs-no-protections-2026-10-01` in
`ft_userdata/preregistrations.json`. Background: the 2026-10-01 ablation (#28):
component C of the 2026-08-23 V2 change (CooldownPeriod + StoplossGuard) was
the most consistent cost, but V2 was designed after seeing that test's OOS, so
only data after 2026-10-01 is clean evidence.

- **Arms** (`FFForward.py`): `FFF_V2` = live FundingFadeV1 (pinned commit
  `f913795`, md5 `f4f70fb0…`); `FFF_V2_noC` = the same without protections.
- **Simulator:** freqtrade backtesting, `--timeframe-detail 1m`, protections on,
  fee 0.075%, the live-matched config in `bt.json` (static 19 pairs, $15 stake,
  2 slots). Calibrated 5/5 against live entries in the 2026-09-30 study.
- **Data:** only trades opened from 2026-10-01. Funding feathers are copied
  read-only from `ft-funding-fade`; OHLCV is downloaded from Binance.
- **Run** (VPS, from this checkout): `research/ff_forward_v2c/run_forward.sh`.
  It prints and stores the scored result under
  `~/master-trader/research/ff_forward_v2c_runs/`.
- **Rule** (`compare.py`): switch live to V2 without C only if, with ≥ 30 closed
  control trades, the challenger's P&L is higher, the 7-day block bootstrap
  gives P ≥ 0.80, and its max drawdown is ≤ 1.25× the control's. Otherwise
  keep V2. Interim looks are descriptive only.

Live trading is unchanged for the whole test.
