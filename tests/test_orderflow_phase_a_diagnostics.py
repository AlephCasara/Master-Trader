"""Quant-Finance regression tests for Phase-A candidate diagnostics."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
FT = ROOT / "ft_userdata"
DIAG = FT / "engine" / "phase_a_diagnostics.py"
POLICY = FT / "research_candidates" / "orderflow_auction_v0_policy.json"
RUNNER = FT / "engine" / "candidate_runner.py"


def _diag_module():
    spec = importlib.util.spec_from_file_location("phase_a_diagnostics_test", DIAG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_effective_cost_is_inferred_from_freqtrade_trade_results():
    diag = _diag_module()
    trades = [
        {"open_rate": 100.0, "close_rate": 101.0, "is_short": False, "profit_ratio": 0.008},
        {"open_rate": 100.0, "close_rate": 99.0, "is_short": True, "profit_ratio": 0.008},
    ]
    # Raw move is 1%; recorded net is 0.8% -> 20 bps effective round-trip cost.
    assert abs(diag.estimate_effective_cost_ratio(trades) - 0.002) < 1e-12


def test_additional_friction_reports_break_even_capacity():
    diag = _diag_module()
    trades = [
        {"profit_ratio": 0.010},
        {"profit_ratio": -0.004},
        {"profit_ratio": 0.006},
    ]
    result = diag.additional_friction_stress(trades, round_trip_bps=(0, 10, 40))
    assert result["measured"] is True
    assert result["trades"] == 3
    assert result["additional_round_trip_break_even_bps"] > 0
    assert [row["additional_round_trip_bps"] for row in result["levels"]] == [0, 10, 40]


def test_multiple_testing_policy_is_preregistered_for_four_candidates():
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    assert policy["kind"] == "research_screen_not_promotion"
    assert policy["planned_signal_trials"] == 4
    assert policy["multiple_testing"]["familywise_alpha"] == 0.05
    assert policy["multiple_testing"]["method"] == "bonferroni"
    assert policy["multiple_testing"]["max_candidate_random_null_p_value"] == 0.0125
    assert policy["boundary"].lower().find("never authorizes live capital") >= 0


def test_runner_integrates_null_friction_and_research_only_quant_screen():
    source = RUNNER.read_text(encoding="utf-8")
    assert "matched_random_null" in source
    assert "additional_friction_stress" in source
    assert "_quant_screen" in source
    assert '"promotion_authority": False' in source
    assert "MAX_ORDERFLOW_CANDLES_PER_PAIR" in source
    assert 'orderflow["max_candles"] = candles' in source
    assert 'orderflow["cache_size"] = candles' in source
