"""Pure regression checks for frozen candidate snapshot coverage logic."""

import importlib.util
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
MODULE = ROOT / "ft_userdata" / "engine" / "data_snapshot_validation.py"
PIPELINE = ROOT / "ft_userdata" / "engine" / "quant_candidate_pipeline.py"


def _module():
    spec = importlib.util.spec_from_file_location("candidate_snapshot_validation", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_numeric_millisecond_trade_timestamps_normalize_to_utc():
    mod = _module()
    series = pd.Series([1767225600000, 1767225900000])
    out = mod._normalize_timestamp_series(series)
    assert str(out.dt.tz) == "UTC"
    assert out.iloc[0] == pd.Timestamp("2026-01-01T00:00:00Z")
    assert out.iloc[1] == pd.Timestamp("2026-01-01T00:05:00Z")


def test_timeframe_tolerance_matches_candle_size():
    mod = _module()
    assert mod._timeframe_delta("1m") == pd.Timedelta(minutes=1)
    assert mod._timeframe_delta("5m") == pd.Timedelta(minutes=5)
    assert mod._timeframe_delta("1h") == pd.Timedelta(hours=1)


def test_canonical_pipeline_invalidates_preliminary_artifact_on_bad_coverage():
    source = PIPELINE.read_text(encoding="utf-8")
    assert "validate_snapshot_coverage" in source
    assert 'result["data_coverage"] = data_coverage' in source
    assert "_invalidate_for_data_coverage" in source
    assert 'screen["passed"] = False' in source
    assert '"data_coverage_passed": True' in source
