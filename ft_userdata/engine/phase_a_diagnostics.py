"""Phase-A diagnostics for fixed-horizon strategy candidates.

These diagnostics do not optimize the strategy. They ask two basic questions:
1) Did the signal choose better opportunities than a randomized process with
   the same pair/side/session exposure and the same 60m/-5% risk contract?
2) How much *additional* round-trip friction erases the observed trade edge?
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def _profit_ratio(trade: dict) -> float | None:
    value = trade.get("profit_ratio")
    if value is None:
        value = trade.get("close_profit")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _raw_price_return(trade: dict) -> float | None:
    try:
        open_rate = float(trade["open_rate"])
        close_rate = float(trade["close_rate"])
    except (KeyError, TypeError, ValueError):
        return None
    if open_rate <= 0:
        return None
    if bool(trade.get("is_short", False)):
        return (open_rate - close_rate) / open_rate
    return (close_rate - open_rate) / open_rate


def estimate_effective_cost_ratio(trades: list[dict]) -> float:
    """Infer baseline round-trip cost from Freqtrade's recorded trade results."""
    costs = []
    for trade in trades:
        raw = _raw_price_return(trade)
        net = _profit_ratio(trade)
        if raw is None or net is None:
            continue
        cost = raw - net
        if np.isfinite(cost) and cost >= 0:
            costs.append(cost)
    return float(np.median(costs)) if costs else 0.0


def additional_friction_stress(
    trades: list[dict],
    round_trip_bps: tuple[int, ...] = (0, 5, 10, 20, 40),
) -> dict:
    """Subtract additional round-trip friction from every recorded trade."""
    base = [p for p in (_profit_ratio(t) for t in trades) if p is not None and np.isfinite(p)]
    if not base:
        return {"measured": False, "reason": "no profit_ratio trades"}

    levels = []
    for bps in round_trip_bps:
        adjusted = np.asarray(base, dtype=float) - (bps / 10_000.0)
        gains = float(adjusted[adjusted > 0].sum())
        losses = float(-adjusted[adjusted < 0].sum())
        pf = gains / losses if losses > 0 else None
        levels.append({
            "additional_round_trip_bps": bps,
            "sum_trade_returns": float(adjusted.sum()),
            "mean_trade_return": float(adjusted.mean()),
            "profit_factor_on_trade_returns": pf,
            "positive_trade_fraction": float((adjusted > 0).mean()),
        })

    return {
        "measured": True,
        "trades": len(base),
        "additional_round_trip_break_even_bps": max(0.0, float(np.mean(base)) * 10_000.0),
        "levels": levels,
    }


def _trade_open_time(trade: dict):
    value = trade.get("open_date") or trade.get("open_date_utc")
    if value is None:
        return None
    stamp = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(stamp) else stamp


def _load_pair_frame(path: Path, timerange: str) -> pd.DataFrame:
    frame = pd.read_feather(path)
    if "date" not in frame.columns:
        raise ValueError(f"OHLCV file has no date column: {path}")
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["date", "open", "high", "low"]).sort_values("date").reset_index(drop=True)
    start_text, end_text = timerange.split("-", 1)
    start = pd.Timestamp(start_text, tz="UTC")
    end = pd.Timestamp(end_text, tz="UTC")
    return frame[(frame["date"] >= start) & (frame["date"] < end)].reset_index(drop=True)


def _random_phase_a_return(
    frame: pd.DataFrame,
    signal_idx: int,
    is_short: bool,
    hold_bars: int,
    stoploss: float,
) -> float:
    """Apply the same next-open, fixed-horizon and catastrophe-stop logic to a random entry."""
    entry_idx = signal_idx + 1
    exit_idx = entry_idx + hold_bars
    entry = float(frame.iloc[entry_idx]["open"])
    window = frame.iloc[entry_idx: exit_idx + 1]

    if is_short:
        stop_rate = entry * (1.0 + abs(stoploss))
        if float(window["high"].max()) >= stop_rate:
            return stoploss
        exit_rate = float(frame.iloc[exit_idx]["open"])
        return (entry - exit_rate) / entry

    stop_rate = entry * (1.0 - abs(stoploss))
    if float(window["low"].min()) <= stop_rate:
        return stoploss
    exit_rate = float(frame.iloc[exit_idx]["open"])
    return (exit_rate - entry) / entry


def matched_random_null(
    trades: list[dict],
    pair_ohlcv_files: dict[str, str],
    timerange: str,
    iterations: int = 1000,
    seed: int = 712_693,
    hold_bars: int = 12,
    stoploss: float = -0.05,
) -> dict:
    """Randomize entry timing while preserving pair, side and UTC hour-of-week.

    Random trades receive the same next-candle-open assumption, 60m horizon and
    catastrophe stop as Phase A. This is a signal-quality null, not a complete
    order-book execution simulator.
    """
    usable = [t for t in trades if t.get("pair") in pair_ohlcv_files and _profit_ratio(t) is not None]
    if not usable:
        return {"measured": False, "reason": "no usable exported trades"}

    frames = {pair: _load_pair_frame(Path(path), timerange) for pair, path in pair_ohlcv_files.items()}
    candidates: dict[tuple[str, int], np.ndarray] = {}
    all_candidates: dict[str, np.ndarray] = {}

    for pair, frame in frames.items():
        eligible = np.arange(0, max(0, len(frame) - hold_bars - 1), dtype=int)
        all_candidates[pair] = eligible
        if len(eligible):
            dates = frame.loc[eligible, "date"]
            how = dates.dt.dayofweek.to_numpy() * 24 + dates.dt.hour.to_numpy()
            for bucket in np.unique(how):
                candidates[(pair, int(bucket))] = eligible[how == bucket]

    estimated_cost = estimate_effective_cost_ratio(usable)
    actual = float(sum(_profit_ratio(t) or 0.0 for t in usable))
    rng = np.random.default_rng(seed)
    null_totals = np.zeros(iterations, dtype=float)
    session_fallbacks = 0
    trade_specs = []

    for trade in usable:
        pair = trade["pair"]
        stamp = _trade_open_time(trade)
        bucket = int(stamp.dayofweek * 24 + stamp.hour) if stamp is not None else None
        pool = candidates.get((pair, bucket)) if bucket is not None else None
        if pool is None or len(pool) == 0:
            pool = all_candidates.get(pair, np.asarray([], dtype=int))
            session_fallbacks += 1
        if len(pool) == 0:
            return {"measured": False, "reason": f"insufficient OHLCV for {pair}"}
        trade_specs.append((pair, bool(trade.get("is_short", False)), pool))

    for iteration in range(iterations):
        total = 0.0
        for pair, is_short, pool in trade_specs:
            signal_idx = int(rng.choice(pool))
            raw = _random_phase_a_return(frames[pair], signal_idx, is_short, hold_bars, stoploss)
            total += raw - estimated_cost
        null_totals[iteration] = total

    percentile = float((null_totals < actual).mean() * 100.0)
    upper_tail_p = float((np.count_nonzero(null_totals >= actual) + 1) / (iterations + 1))
    return {
        "measured": True,
        "method": "pair_side_hour_of_week_matched_random_phase_a",
        "iterations": iterations,
        "trade_count": len(usable),
        "hold_bars": hold_bars,
        "stoploss": stoploss,
        "estimated_effective_round_trip_cost_ratio": estimated_cost,
        "actual_sum_trade_returns": actual,
        "null_median_sum_trade_returns": float(np.median(null_totals)),
        "null_p95_sum_trade_returns": float(np.percentile(null_totals, 95)),
        "percentile_vs_null": percentile,
        "upper_tail_p_value": upper_tail_p,
        "session_fallback_count": session_fallbacks,
    }
