"""Shared executable contract for research-only order-flow auction candidates."""

from __future__ import annotations

from datetime import datetime

from freqtrade.strategy import IStrategy
from pandas import DataFrame

from orderflow_auction_common import add_orderflow_research_features


class OrderflowAuctionBase(IStrategy):
    """Common entry/exit/risk plumbing. Subclasses only choose an entry hypothesis."""

    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "5m"
    process_only_new_candles = True
    startup_candle_count = 128

    # Phase A deliberately does not optimize exits. The signal is evaluated on
    # a common 60-minute holding horizon, with a catastrophe floor only.
    minimal_roi = {"0": 10.0}
    stoploss = -0.05
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    order_types = {
        "entry": "market",
        "exit": "market",
        "emergency_exit": "market",
        "force_entry": "market",
        "force_exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    RESEARCH_HORIZON_MINUTES = 60
    RESEARCH_LEVERAGE = 1.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return add_orderflow_research_features(dataframe)

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # No price/flow exit is optimized in Phase A. custom_exit() supplies the
        # fixed horizon; stoploss supplies catastrophic protection.
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def custom_exit(
        self,
        pair: str,
        trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ):
        age_minutes = (current_time - trade.open_date_utc).total_seconds() / 60.0
        if age_minutes >= self.RESEARCH_HORIZON_MINUTES:
            return "phase_a_60m"
        return None

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        return min(self.RESEARCH_LEVERAGE, max_leverage)
