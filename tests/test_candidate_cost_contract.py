"""Regression contract for reproducible market-order fees in candidate research."""

import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
MANIFEST = ROOT / "ft_userdata" / "research_candidates" / "orderflow_auction_v0.json"
PIPELINE = ROOT / "ft_userdata" / "engine" / "quant_candidate_pipeline.py"


def test_orderflow_v0_freezes_conservative_market_fee():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cost = manifest["cost_contract"]
    assert cost["fee_ratio_per_side"] == 0.0005
    assert cost["fee_bps_per_side"] == 5.0
    assert cost["round_trip_fee_bps"] == 10.0
    assert cost["additional_round_trip_friction_stress_bps"] == [0, 5, 10, 20, 40]
    assert manifest["execution_contract"]["entry_order_type"] == "market"
    assert manifest["execution_contract"]["exit_order_type"] == "market"


def test_top_level_pipeline_injects_fee_into_generated_freqtrade_configs():
    source = PIPELINE.read_text(encoding="utf-8")
    assert "cost_contract.fee_ratio_per_side" in source
    assert 'config_builder.BASE_CONFIG["fee"] = fee' in source
    assert '"cost_contract": cost_contract' in source
