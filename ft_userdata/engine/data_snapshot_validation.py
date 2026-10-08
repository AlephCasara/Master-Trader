"""Coverage validation for frozen candidate OHLCV and raw-trade snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


def _timerange(timerange: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    if timerange.count("-") != 1:
        raise ValueError("timerange must be YYYYMMDD-YYYYMMDD")
    start_s, end_s = timerange.split("-", 1)
    start = pd.Timestamp(datetime.strptime(start_s, "%Y%m%d"), tz="UTC")
    end = pd.Timestamp(datetime.strptime(end_s, "%Y%m%d"), tz="UTC")
    if end <= start:
        raise ValueError("timerange end must be after start")
    return start, end


def _normalize_timestamp_series(series: pd.Series) -> pd.Series:
    """Normalize datetime or numeric exchange timestamps to UTC."""
    if pd.api.types.is_numeric_dtype(series):
        clean = pd.to_numeric(series, errors="coerce").dropna()
        if clean.empty:
            return pd.to_datetime(series, utc=True, errors="coerce")
        magnitude = float(clean.abs().median())
        if magnitude >= 1e17:
            unit = "ns"
        elif magnitude >= 1e14:
            unit = "us"
        elif magnitude >= 1e11:
            unit = "ms"
        else:
            unit = "s"
        return pd.to_datetime(series, unit=unit, utc=True, errors="coerce")
    return pd.to_datetime(series, utc=True, errors="coerce")


def _feather_bounds(path: Path) -> tuple[pd.Timestamp, pd.Timestamp, str]:
    """Read only a time column when possible and return min/max UTC bounds."""
    errors = []
    for column in ("date", "timestamp"):
        try:
            frame = pd.read_feather(path, columns=[column])
        except Exception as exc:  # schema/engine errors are reported after both candidates
            errors.append(f"{column}: {exc}")
            continue
        if column not in frame.columns:
            continue
        stamps = _normalize_timestamp_series(frame[column]).dropna()
        if not stamps.empty:
            return stamps.min(), stamps.max(), column
    raise ValueError(f"cannot read timestamp coverage from {path}: {'; '.join(errors)}")


def _timeframe_delta(timeframe: str) -> pd.Timedelta:
    if timeframe.endswith("m"):
        return pd.Timedelta(minutes=int(timeframe[:-1]))
    if timeframe.endswith("h"):
        return pd.Timedelta(hours=int(timeframe[:-1]))
    if timeframe.endswith("d"):
        return pd.Timedelta(days=int(timeframe[:-1]))
    raise ValueError(f"unsupported timeframe for coverage check: {timeframe}")


def _infer_timeframe(path: Path, allowed: list[str]) -> str:
    name = path.name
    matches = [tf for tf in allowed if f"-{tf}-" in name or name.endswith(f"-{tf}.feather")]
    if len(matches) != 1:
        raise ValueError(f"cannot infer timeframe for {path.name} from {allowed}")
    return matches[0]


def validate_snapshot_coverage(
    data_contract: dict,
    timerange: str,
    timeframes: list[str],
    raw_trade_tolerance: str = "5min",
) -> dict:
    """Verify every frozen file reaches both boundaries of the decision range.

    OHLCV files are allowed to end one candle before the exclusive range end.
    Raw trades receive a small tolerance because the last trade need not occur
    exactly at midnight. Any missing/short file makes the report fail closed.
    """
    start, end = _timerange(timerange)
    checks = []

    for pair, paths in sorted((data_contract.get("ohlcv_files") or {}).items()):
        for path_text in paths:
            path = Path(path_text)
            tf = _infer_timeframe(path, timeframes)
            tolerance = _timeframe_delta(tf)
            try:
                first, last, column = _feather_bounds(path)
                passed = first <= start and last >= end - tolerance
                error = None
            except Exception as exc:
                first = last = column = None
                passed = False
                error = str(exc)
            checks.append({
                "kind": "ohlcv",
                "pair": pair,
                "timeframe": tf,
                "path": str(path),
                "first": str(first) if first is not None else None,
                "last": str(last) if last is not None else None,
                "timestamp_column": column,
                "required_start_lte": str(start),
                "required_end_gte": str(end - tolerance),
                "passed": passed,
                "error": error,
            })

    trade_tolerance = pd.Timedelta(raw_trade_tolerance)
    for pair, path_text in sorted((data_contract.get("raw_trade_files") or {}).items()):
        path = Path(path_text)
        try:
            first, last, column = _feather_bounds(path)
            passed = first <= start + trade_tolerance and last >= end - trade_tolerance
            error = None
        except Exception as exc:
            first = last = column = None
            passed = False
            error = str(exc)
        checks.append({
            "kind": "raw_trades",
            "pair": pair,
            "path": str(path),
            "first": str(first) if first is not None else None,
            "last": str(last) if last is not None else None,
            "timestamp_column": column,
            "required_start_lte": str(start + trade_tolerance),
            "required_end_gte": str(end - trade_tolerance),
            "passed": passed,
            "error": error,
        })

    expected = sum(len(paths) for paths in (data_contract.get("ohlcv_files") or {}).values())
    expected += len(data_contract.get("raw_trade_files") or {})
    passed_count = sum(1 for check in checks if check["passed"])
    return {
        "method": "frozen_snapshot_boundary_coverage",
        "timerange": timerange,
        "checks_expected": expected,
        "checks_measured": len(checks),
        "checks_passed": passed_count,
        "passed": expected > 0 and len(checks) == expected and passed_count == expected,
        "checks": checks,
    }


def require_snapshot_coverage(data_contract: dict, timerange: str, timeframes: list[str]) -> dict:
    report = validate_snapshot_coverage(data_contract, timerange, timeframes)
    if not report["passed"]:
        failed = [
            f"{row['kind']}:{row['pair']}:{row.get('timeframe') or ''}"
            for row in report["checks"]
            if not row["passed"]
        ]
        raise RuntimeError(f"candidate snapshot does not cover frozen timerange: {failed}")
    return report
