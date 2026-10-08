"""AuctionExpansionV0 — first channel expansion confirmed by public-trade flow."""

from pandas import DataFrame

from orderflow_auction_base import OrderflowAuctionBase
from orderflow_auction_common import expansion_long, expansion_short


class AuctionExpansionV0(OrderflowAuctionBase):
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        long_mask = expansion_long(dataframe)
        short_mask = expansion_short(dataframe)
        dataframe.loc[long_mask, ["enter_long", "enter_tag"]] = (1, "auction_expansion_long")
        dataframe.loc[short_mask, ["enter_short", "enter_tag"]] = (1, "auction_expansion_short")
        return dataframe
