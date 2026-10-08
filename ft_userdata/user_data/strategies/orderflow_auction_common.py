"""Causal feature construction for the research-only order-flow auction family."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from pandas import DataFrame, Series

CHANNEL_BARS = 24
FLOW_BASELINE_BARS = 96
FLOW_Z_THRESHOLD = 1.5


def _reported_side_flow(trades: Any) -> tuple[float, float, float]:
    """Return (buy_volume, sell_volume, normalized_imbalance) for one candle.

    Freqtrade's public-trades dataframe column contains a list of trade dicts
    with `amount` and reported `side`. We deliberately do not infer participant
    identity, inventory, queue position or a trapped-player narrative.
    """
    if not isinstance(trades, (list, tuple)) or not trades:
        return 0.0, 0.0, np.nan

    buy_volume = 0.0
    sell_volume = 0.0
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        try:
            amount = float(trade.get("amount", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(amount) or amount <= 0:
            continue
        side = str(trade.get("side", "")).lower()
        if side == "buy":
            buy_volume += amount
        elif side == "sell":
            sell_volume += amount

    total = buy_volume + sell_volume
    if total <= 0:
        return buy_volume, sell_volume, np.nan
    return buy_volume, sell_volume, (buy_volume - sell_volume) / total


def add_orderflow_research_features(dataframe: DataFrame) -> DataFrame:
    """Add completed-channel and raw-public-trade features.

    Price-channel and flow baseline use completed prior candles. The current
    candle's raw public trades are allowed only to produce a signal that is
    acted on after the candle has closed.

    Missing raw trades fail closed: `of_ready` remains 0 and all signal helpers
    below require `of_ready == 1`.
    """
    df = dataframe.copy()

    df["channel_high"] = df["high"].shift(1).rolling(CHANNEL_BARS, min_periods=CHANNEL_BARS).max()
    df["channel_low"] = df["low"].shift(1).rolling(CHANNEL_BARS, min_periods=CHANNEL_BARS).min()

    if "trades" not in df.columns:
        df["reported_buy_volume"] = np.nan
        df["reported_sell_volume"] = np.nan
        df["flow_imbalance"] = np.nan
        df["flow_z"] = np.nan
        df["of_ready"] = 0
        return df

    flow = df["trades"].apply(_reported_side_flow)
    df["reported_buy_volume"] = flow.map(lambda item: item[0])
    df["reported_sell_volume"] = flow.map(lambda item: item[1])
    df["flow_imbalance"] = flow.map(lambda item: item[2])

    prior_mean = (
        df["flow_imbalance"].shift(1).rolling(FLOW_BASELINE_BARS, min_periods=FLOW_BASELINE_BARS).mean()
    )
    prior_std = (
        df["flow_imbalance"].shift(1).rolling(FLOW_BASELINE_BARS, min_periods=FLOW_BASELINE_BARS).std(ddof=0)
    )
    safe_std = prior_std.where(prior_std > 1e-12)
    df["flow_z"] = (df["flow_imbalance"] - prior_mean) / safe_std

    df["of_ready"] = (
        df["flow_imbalance"].notna()
        & df["flow_z"].notna()
        & df["channel_high"].notna()
        & df["channel_low"].notna()
    ).astype(int)
    return df


def expansion_long(df: DataFrame) -> Series:
    return (
        (df["of_ready"] == 1)
        & (df["close"] > df["channel_high"])
        & (df["close"].shift(1) <= df["channel_high"].shift(1))
        & (df["flow_z"] >= FLOW_Z_THRESHOLD)
    )


def expansion_short(df: DataFrame) -> Series:
    return (
        (df["of_ready"] == 1)
        & (df["close"] < df["channel_low"])
        & (df["close"].shift(1) >= df["channel_low"].shift(1))
        & (df["flow_z"] <= -FLOW_Z_THRESHOLD)
    )


def retest_long(df: DataFrame) -> Series:
    old_boundary = df["channel_high"].shift(1)
    prior_break = df["close"].shift(1) > old_boundary
    return (
        (df["of_ready"] == 1)
        & prior_break
        & (df["low"] <= old_boundary)
        & (df["close"] > old_boundary)
        & (df["flow_z"] >= FLOW_Z_THRESHOLD)
    )


def retest_short(df: DataFrame) -> Series:
    old_boundary = df["channel_low"].shift(1)
    prior_break = df["close"].shift(1) < old_boundary
    return (
        (df["of_ready"] == 1)
        & prior_break
        & (df["high"] >= old_boundary)
        & (df["close"] < old_boundary)
        & (df["flow_z"] <= -FLOW_Z_THRESHOLD)
    )


def failed_auction_long(df: DataFrame) -> Series:
    return (
        (df["of_ready"] == 1)
        & (df["close"].shift(1) >= df["channel_low"].shift(1))
        & (df["low"] < df["channel_low"])
        & (df["close"] > df["channel_low"])
    )


def failed_auction_short(df: DataFrame) -> Series:
    return (
        (df["of_ready"] == 1)
        & (df["close"].shift(1) <= df["channel_high"].shift(1))
        & (df["high"] > df["channel_high"])
        & (df["close"] < df["channel_high"])
    )


def absorption_long(df: DataFrame) -> Series:
    return failed_auction_long(df) & (df["flow_z"] <= -FLOW_Z_THRESHOLD)


def absorption_short(df: DataFrame) -> Series:
    return failed_auction_short(df) & (df["flow_z"] >= FLOW_Z_THRESHOLD)
