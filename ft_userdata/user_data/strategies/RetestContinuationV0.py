"""RetestContinuationV0 — second-drive continuation after a confirmed breakout."""

from pandas import DataFrame

from orderflow_auction_base import OrderflowAuctionBase
from orderflow_auction_common import retest_long, retest_short


class RetestContinuationV0(OrderflowAuctionBase):
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        long_mask = retest_long(dataframe)
        short_mask = retest_short(dataframe)
        dataframe.loc[long_mask, ["enter_long", "enter_tag"]] = (1, "retest_continuation_long")
        dataframe.loc[short_mask, ["enter_short", "enter_tag"]] = (1, "retest_continuation_short")
        return dataframe
