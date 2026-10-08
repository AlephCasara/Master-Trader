"""FailedAuctionReversionV0 — price-only failed channel excursion baseline."""

from pandas import DataFrame

from orderflow_auction_base import OrderflowAuctionBase
from orderflow_auction_common import failed_auction_long, failed_auction_short


class FailedAuctionReversionV0(OrderflowAuctionBase):
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        long_mask = failed_auction_long(dataframe)
        short_mask = failed_auction_short(dataframe)
        dataframe.loc[long_mask, ["enter_long", "enter_tag"]] = (1, "failed_auction_long")
        dataframe.loc[short_mask, ["enter_short", "enter_tag"]] = (1, "failed_auction_short")
        return dataframe
