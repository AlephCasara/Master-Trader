"""Backtest-only variants of OITrendPullbackV1 for issue #18. NOT for live use.

Each variant inherits the deployed strategy unchanged and replaces only the
``oi_growth`` column that the entry term ``oi_growth >= oi_min_growth`` reads:

* OITrendPullbackV1CausalOI  - causal 15m openInterestHist growth joined at
  each candle's decision time (oi_history.py align); missing = NaN = no entry.
* OITrendPullbackV1PriceOnly - +inf, so the OI term always passes. Identical
  TA, execution, protections, ROI/stop/trailing and custom_exit otherwise.
* OITrendPullbackV1Placebo01..20 - a seeded coin flip per (pair, candle) that
  passes with probability PLACEBO_PASS_RATE, the null a real gate must beat.

Copy this file next to the deployed OITrendPullbackV1.py in a research
user_data/strategies directory and point OI_CAUSAL_DIR at the aligned CSVs.
"""
import csv
import hashlib
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from OITrendPullbackV1 import OITrendPullbackV1  # noqa: E402

PLACEBO_PASS_RATE = 0.5  # unconditional share of causal 45m OI growth >= 0 (2026-09-03..10-02: 50.0%)
_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


def _date_ms(dates: pd.Series) -> pd.Series:
    return (dates - _EPOCH) // pd.Timedelta(milliseconds=1)


class _BacktestOnly(OITrendPullbackV1):
    def _assert_backtest(self):
        if self._uses_live_oi():
            raise RuntimeError(f"{type(self).__name__} is a research backtest variant; never run it live/dry")

    def _replace_oi(self, dataframe: pd.DataFrame, pair: str) -> pd.Series:
        raise NotImplementedError

    def populate_indicators(self, dataframe, metadata):
        self._assert_backtest()
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["oi_growth"] = self._replace_oi(dataframe, metadata["pair"]).to_numpy()
        return dataframe


class OITrendPullbackV1CausalOI(_BacktestOnly):
    _cache: dict = {}

    def _load(self, pair):
        symbol = pair.split("/")[0] + "USDT"
        if symbol not in self._cache:
            root = Path(os.environ.get("OI_CAUSAL_DIR", "/freqtrade/user_data/oi_causal"))
            table = {}
            with open(root / f"{symbol}-oi_growth_causal.csv") as f:
                for r in csv.DictReader(f):
                    table[int(r["date_ms"])] = float(r["oi_growth"]) if r["oi_growth"] else np.nan
            self._cache[symbol] = table
        return self._cache[symbol]

    def _replace_oi(self, dataframe, pair):
        table = self._load(pair)
        return _date_ms(dataframe["date"]).map(lambda t: table.get(int(t), np.nan)).astype(float)


class OITrendPullbackV1PriceOnly(_BacktestOnly):
    def _replace_oi(self, dataframe, pair):
        return pd.Series(np.inf, index=dataframe.index)


class _Placebo(_BacktestOnly):
    placebo_seed = 0

    def _replace_oi(self, dataframe, pair):
        def draw(t):
            h = hashlib.sha256(f"{self.placebo_seed}|{pair}|{int(t)}".encode()).digest()
            u = int.from_bytes(h[:8], "big") / 2 ** 64
            return 1.0 if u < PLACEBO_PASS_RATE else -1.0
        return _date_ms(dataframe["date"]).map(draw).astype(float)


for _i in range(1, 21):
    _name = f"OITrendPullbackV1Placebo{_i:02d}"
    globals()[_name] = type(_name, (_Placebo,), {"placebo_seed": _i})
