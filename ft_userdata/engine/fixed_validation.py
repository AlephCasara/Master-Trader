"""Fixed-parameter chronological validation for preregistered candidates.

Unlike walk-forward optimization this module NEVER selects parameters. It asks
whether one frozen strategy behaves consistently across chronological slices.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

from .config_builder import build_backtest_config
from .viability import run_full_backtest


def _parse_closed_timerange(timerange: str) -> tuple[datetime, datetime]:
    if timerange.count("-") != 1:
        raise ValueError("timerange must be a closed YYYYMMDD-YYYYMMDD range")
    start_text, end_text = timerange.split("-", 1)
    if len(start_text) != 8 or len(end_text) != 8:
        raise ValueError("timerange must be a closed YYYYMMDD-YYYYMMDD range")
    start = datetime.strptime(start_text, "%Y%m%d")
    end = datetime.strptime(end_text, "%Y%m%d")
    if end <= start:
        raise ValueError("timerange end must be after start")
    return start, end


def chronological_windows(timerange: str, windows: int) -> list[str]:
    """Split a closed range into non-overlapping chronological windows."""
    if windows < 2:
        raise ValueError("windows must be >= 2")
    start, end = _parse_closed_timerange(timerange)
    total_days = (end - start).days
    if total_days < windows * 7:
        raise ValueError("timerange is too short: require at least 7 days per window")

    boundaries = [start + timedelta(days=(total_days * i) // windows) for i in range(windows)]
    boundaries.append(end)
    return [
        f"{boundaries[i].strftime('%Y%m%d')}-{boundaries[i + 1].strftime('%Y%m%d')}"
        for i in range(windows)
    ]


def run_fixed_parameter_validation(
    strategy_name: str,
    pairs: list[str],
    timerange: str,
    windows: int = 6,
) -> dict:
    """Run the exact same strategy over consecutive non-overlapping windows."""
    ranges = chronological_windows(timerange, windows)
    config_path = build_backtest_config(strategy_name, pairs)
    results = []

    for window in ranges:
        metrics = run_full_backtest(strategy_name, window, config_path)
        results.append(
            {
                "timerange": window,
                "total_trades": metrics.get("total_trades"),
                "total_profit": metrics.get("total_profit"),
                "profit_factor": metrics.get("profit_factor"),
                "max_drawdown_pct": metrics.get("max_drawdown_pct"),
                "sharpe": metrics.get("sharpe"),
                "error": metrics.get("error"),
            }
        )

    measured = [r for r in results if not r.get("error") and r.get("total_trades") is not None]
    profitable = [r for r in measured if (r.get("total_profit") or 0.0) > 0]
    pfs = [float(r["profit_factor"]) for r in measured if r.get("profit_factor") is not None]
    dds = [float(r["max_drawdown_pct"]) for r in measured if r.get("max_drawdown_pct") is not None]

    return {
        "method": "fixed_parameter_temporal_stability",
        "optimization_performed": False,
        "strategy": strategy_name,
        "source_timerange": timerange,
        "windows": results,
        "windows_requested": windows,
        "windows_measured": len(measured),
        "profitable_windows": len(profitable),
        "total_trades": sum(int(r.get("total_trades") or 0) for r in measured),
        "median_profit_factor": median(pfs) if pfs else None,
        "worst_drawdown_pct": max(dds) if dds else None,
    }
