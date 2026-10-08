"""AbsorptionReversalV0 — failed auction conditioned on extreme opposite flow."""

from pandas import DataFrame

from orderflow_auction_base import OrderflowAuctionBase
from orderflow_auction_common import absorption_long, absorption_short


class AbsorptionReversalV0(OrderflowAuctionBase):
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        long_mask = absorption_long(dataframe)
        short_mask = absorption_short(dataframe)
        dataframe.loc[long_mask, ["enter_long", "enter_tag"]] = (1, "absorption_reversal_long")
        dataframe.loc[short_mask, ["enter_short", "enter_tag"]] = (1, "absorption_reversal_short")
        return dataframe
