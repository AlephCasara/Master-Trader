"""Small deterministic checks for the Phase-A matched random benchmark."""

import importlib.util
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
MODULE = ROOT / "ft_userdata" / "engine" / "phase_a_diagnostics.py"


def _module():
    spec = importlib.util.spec_from_file_location("phase_a_random_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_random_long_applies_same_catastrophe_stop_before_horizon():
    mod = _module()
    frame = pd.DataFrame({
        "open": [100.0] * 20,
        "high": [101.0] * 20,
        "low": [99.0] * 20,
    })
    frame.loc[4, "low"] = 94.0
    assert mod._random_phase_a_return(frame, signal_idx=0, is_short=False, hold_bars=12, stoploss=-0.05) == -0.05


def test_random_short_applies_same_catastrophe_stop_before_horizon():
    mod = _module()
    frame = pd.DataFrame({
        "open": [100.0] * 20,
        "high": [101.0] * 20,
        "low": [99.0] * 20,
    })
    frame.loc[4, "high"] = 106.0
    assert mod._random_phase_a_return(frame, signal_idx=0, is_short=True, hold_bars=12, stoploss=-0.05) == -0.05
