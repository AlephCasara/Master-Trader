"""Deterministic trade diagnostics for research candidate decision artifacts.

This module summarizes exported Freqtrade trades without optimizing or changing
strategy behavior.  It is deliberately descriptive: breadth/concentration are
reported first and can become gates only in a separately preregistered policy.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from statistics import median
from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _profit_ratio(trade: dict) -> float:
    value = trade.get("profit_ratio")
    if value is None:
        value = trade.get("close_profit")
    return _number(value)


def _profit_abs(trade: dict) -> float:
    value = trade.get("profit_abs")
    if value is None:
        value = trade.get("profit_amount")
    return _number(value)


def _stamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _timerange_bounds(timerange: str) -> tuple[datetime, datetime]:
    if timerange.count("-") != 1:
        raise ValueError("timerange must be YYYYMMDD-YYYYMMDD")
    start_s, end_s = timerange.split("-", 1)
    start = datetime.strptime(start_s, "%Y%m%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(end_s, "%Y%m%d").replace(tzinfo=timezone.utc)
    if end <= start:
        raise ValueError("timerange end must be after start")
    return start, end


def _interval_union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    if not intervals:
        return 0.0
    ordered = sorted(intervals, key=lambda item: item[0])
    cur_start, cur_end = ordered[0]
    total = 0.0
    for start, end in ordered[1:]:
        if start <= cur_end:
            if end > cur_end:
                cur_end = end
            continue
        total += (cur_end - cur_start).total_seconds()
        cur_start, cur_end = start, end
    total += (cur_end - cur_start).total_seconds()
    return max(0.0, total)


def summarize_trades(trades: list[dict], timerange: str) -> dict:
    """Build the minimum decision diagnostics promised by candidate manifests.

    Turnover is reported as gross entry+exit notional when Freqtrade exported a
    stake amount.  Time-in-market has two views: union occupancy (at least one
    position open) and gross position-hours (which intentionally counts
    concurrent positions separately).
    """
    start, end = _timerange_bounds(timerange)
    period_seconds = (end - start).total_seconds()
    if not trades:
        return {
            "measured": False,
            "reason": "no exported trades",
            "total_trades": 0,
            "assets_traded": 0,
        }

    by_asset: dict[str, dict] = {}
    side_count = Counter()
    side_profit_ratio: dict[str, float] = {"long": 0.0, "short": 0.0}
    exit_reasons = Counter()
    durations_minutes: list[float] = []
    intervals: list[tuple[datetime, datetime]] = []
    turnover_usdt = 0.0
    turnover_observations = 0

    for trade in trades:
        pair = str(trade.get("pair") or "UNKNOWN")
        side = "short" if bool(trade.get("is_short", False)) else "long"
        ratio = _profit_ratio(trade)
        absolute = _profit_abs(trade)
        side_count[side] += 1
        side_profit_ratio[side] += ratio
        exit_reasons[str(trade.get("exit_reason") or "UNKNOWN")] += 1

        asset = by_asset.setdefault(
            pair,
            {
                "pair": pair,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "sum_profit_ratio": 0.0,
                "sum_profit_abs": 0.0,
            },
        )
        asset["trades"] += 1
        asset["sum_profit_ratio"] += ratio
        asset["sum_profit_abs"] += absolute
        if ratio > 0:
            asset["wins"] += 1
        elif ratio < 0:
            asset["losses"] += 1

        opened = _stamp(trade.get("open_date") or trade.get("open_date_utc"))
        closed = _stamp(trade.get("close_date") or trade.get("close_date_utc"))
        if opened is not None and closed is not None and closed >= opened:
            clipped_start = max(opened, start)
            clipped_end = min(closed, end)
            if clipped_end >= clipped_start:
                intervals.append((clipped_start, clipped_end))
                durations_minutes.append((closed - opened).total_seconds() / 60.0)

        stake = trade.get("stake_amount")
        if stake is not None:
            leverage = max(1.0, _number(trade.get("leverage"), 1.0))
            turnover_usdt += 2.0 * max(0.0, _number(stake)) * leverage
            turnover_observations += 1

    per_asset = sorted(by_asset.values(), key=lambda row: (row["sum_profit_ratio"], row["pair"]))
    for row in per_asset:
        row["win_rate"] = row["wins"] / row["trades"] if row["trades"] else 0.0

    positive_total = sum(max(0.0, row["sum_profit_ratio"]) for row in per_asset)
    best_positive = max((max(0.0, row["sum_profit_ratio"]) for row in per_asset), default=0.0)
    top_positive_concentration = best_positive / positive_total if positive_total > 0 else None

    gross_seconds = sum((close - open_).total_seconds() for open_, close in intervals)
    union_seconds = _interval_union_seconds(intervals)
    sorted_durations = sorted(durations_minutes)
    p95_idx = max(0, int(round(0.95 * (len(sorted_durations) - 1)))) if sorted_durations else 0

    return {
        "measured": True,
        "total_trades": len(trades),
        "assets_traded": len(per_asset),
        "per_asset": per_asset,
        "worst_asset": per_asset[0] if per_asset else None,
        "best_asset": per_asset[-1] if per_asset else None,
        "top_positive_pnl_concentration": top_positive_concentration,
        "long_short_split": {
            "long_trades": side_count["long"],
            "short_trades": side_count["short"],
            "long_sum_profit_ratio": side_profit_ratio["long"],
            "short_sum_profit_ratio": side_profit_ratio["short"],
        },
        "exit_reasons": dict(sorted(exit_reasons.items())),
        "duration_minutes": {
            "measured_trades": len(sorted_durations),
            "median": median(sorted_durations) if sorted_durations else None,
            "p95": sorted_durations[p95_idx] if sorted_durations else None,
            "max": max(sorted_durations) if sorted_durations else None,
        },
        "time_in_market": {
            "union_fraction": union_seconds / period_seconds if period_seconds > 0 else None,
            "gross_position_hours": gross_seconds / 3600.0,
        },
        "turnover": {
            "gross_entry_exit_notional_usdt": turnover_usdt if turnover_observations else None,
            "trades_with_stake_amount": turnover_observations,
            "definition": "2 * stake_amount * leverage per closed trade; descriptive, not exchange-volume reconstruction",
        },
    }
