"""Regression tests for the automated research-only order-flow candidates."""

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
FT = ROOT / "ft_userdata"
STRATEGIES = FT / "user_data" / "strategies"
MANIFEST = FT / "research_candidates" / "orderflow_auction_v0.json"
BOTS = FT / "bots_config.json"

CANDIDATES = (
    "AuctionExpansionV0",
    "RetestContinuationV0",
    "FailedAuctionReversionV0",
    "AbsorptionReversalV0",
)


def _strategy_path():
    path = str(STRATEGIES)
    if path not in sys.path:
        sys.path.insert(0, path)


def _common():
    _strategy_path()
    sys.modules.pop("orderflow_auction_common", None)
    return importlib.import_module("orderflow_auction_common")


def _install_freqtrade_stub():
    freqtrade = ModuleType("freqtrade")
    strategy = ModuleType("freqtrade.strategy")
    strategy.IStrategy = object
    freqtrade.strategy = strategy
    sys.modules["freqtrade"] = freqtrade
    sys.modules["freqtrade.strategy"] = strategy


def _frame(rows=150):
    # Vary prior reported flow so rolling std is non-zero.
    trade_rows = []
    for i in range(rows):
        buy = 5.0 + float(np.sin(i / 3.0))
        sell = 5.0 - float(np.sin(i / 3.0))
        trade_rows.append([
            {"amount": buy, "side": "buy", "price": 100.0},
            {"amount": sell, "side": "sell", "price": 100.0},
        ])
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC"),
        "open": np.full(rows, 100.0),
        "high": np.full(rows, 101.0),
        "low": np.full(rows, 99.0),
        "close": np.full(rows, 100.0),
        "volume": np.full(rows, 10.0),
        "trades": trade_rows,
    })


def test_missing_public_trades_fails_closed():
    common = _common()
    frame = _frame().drop(columns=["trades"])
    features = common.add_orderflow_research_features(frame)
    assert features["of_ready"].sum() == 0
    assert not common.expansion_long(features).any()
    assert not common.failed_auction_long(features).any()


def test_channel_is_completed_prior_bars_only():
    common = _common()
    frame = _frame()
    baseline = common.add_orderflow_research_features(frame.copy())
    changed = frame.copy()
    changed.loc[130, ["high", "low"]] = [10000.0, 1.0]
    after = common.add_orderflow_research_features(changed)
    assert baseline.loc[130, "channel_high"] == after.loc[130, "channel_high"] == 101.0
    assert baseline.loc[130, "channel_low"] == after.loc[130, "channel_low"] == 99.0


def test_expansion_requires_extreme_same_direction_reported_flow():
    common = _common()
    frame = _frame()
    idx = 130
    frame.loc[idx, ["high", "close"]] = [103.0, 102.0]
    frame.at[idx, "trades"] = [
        {"amount": 20.0, "side": "buy", "price": 102.0},
        {"amount": 0.1, "side": "sell", "price": 102.0},
    ]
    features = common.add_orderflow_research_features(frame)
    assert features.loc[idx, "flow_z"] >= common.FLOW_Z_THRESHOLD
    assert common.expansion_long(features).loc[idx]


def test_retest_uses_original_prebreak_boundary():
    common = _common()
    frame = _frame()
    idx = 130
    frame.loc[idx - 1, ["high", "close"]] = [103.0, 102.0]
    frame.loc[idx, ["low", "high", "close"]] = [100.5, 102.0, 101.5]
    frame.at[idx, "trades"] = [
        {"amount": 20.0, "side": "buy", "price": 101.5},
        {"amount": 0.1, "side": "sell", "price": 101.5},
    ]
    features = common.add_orderflow_research_features(frame)
    assert features.loc[idx - 1, "channel_high"] == 101.0
    assert common.retest_long(features).loc[idx]


def test_absorption_is_flow_conditioned_failed_auction_ablation():
    common = _common()
    frame = _frame()
    idx = 130
    frame.loc[idx, ["low", "close"]] = [98.0, 99.5]
    neutral = common.add_orderflow_research_features(frame.copy())
    assert common.failed_auction_long(neutral).loc[idx]
    assert not common.absorption_long(neutral).loc[idx]

    frame.at[idx, "trades"] = [
        {"amount": 0.1, "side": "buy", "price": 99.5},
        {"amount": 20.0, "side": "sell", "price": 99.5},
    ]
    extreme = common.add_orderflow_research_features(frame)
    assert common.failed_auction_long(extreme).loc[idx]
    assert common.absorption_long(extreme).loc[idx]


def test_all_candidates_have_automated_entry_and_common_exit_risk_contract():
    _strategy_path()
    _install_freqtrade_stub()
    for module_name in ("orderflow_auction_common", "orderflow_auction_base", *CANDIDATES):
        sys.modules.pop(module_name, None)
    base = importlib.import_module("orderflow_auction_base")
    assert base.OrderflowAuctionBase.can_short is True
    assert base.OrderflowAuctionBase.timeframe == "5m"
    assert base.OrderflowAuctionBase.stoploss == -0.05
    assert base.OrderflowAuctionBase.trailing_stop is False
    assert base.OrderflowAuctionBase.order_types["entry"] == "market"
    assert base.OrderflowAuctionBase.order_types["exit"] == "market"
    assert base.OrderflowAuctionBase.RESEARCH_HORIZON_MINUTES == 60

    trade = SimpleNamespace(open_date_utc=datetime.now(timezone.utc) - timedelta(minutes=61))
    strategy = base.OrderflowAuctionBase()
    assert strategy.custom_exit("BTC/USDT:USDT", trade, datetime.now(timezone.utc), 100, 0) == "phase_a_60m"

    for name in CANDIDATES:
        module = importlib.import_module(name)
        cls = getattr(module, name)
        assert "populate_entry_trend" in cls.__dict__


def test_manifest_freezes_assets_and_never_registers_runtime_bots():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bots = json.loads(BOTS.read_text(encoding="utf-8"))["bots"]
    assert manifest["status"] == "research_only"
    assert manifest["runtime_authority"] is False
    assert manifest["development_universe"]["dynamic_pairlist_forbidden"] is True
    assert len(manifest["development_universe"]["pairs"]) == 8
    assert manifest["phase_a"]["planned_signal_trials"] == 4
    assert manifest["signal_contract"]["signal_hyperopt_allowed"] is False
    assert manifest["execution_contract"]["phase_a_time_exit_minutes"] == 60
    for name in CANDIDATES:
        assert name not in bots


def test_candidate_runner_uses_in_memory_candidate_status_and_quant_stages():
    source = (FT / "engine" / "candidate_runner.py").read_text(encoding="utf-8")
    assert '"status": "candidate"' in source
    assert "registry.STRATEGIES[name] =" in source
    assert "registry.get_active_strategies()" in source
    assert "run_viability_stage" in source
    assert "run_fixed_parameter_validation" in source
    assert "run_robustness_stage" in source
    assert "perturb_pcts\"] = []" in source
    assert "bots_config" not in source


def test_fixed_validation_is_non_optimizing_and_chronological():
    source = (FT / "engine" / "fixed_validation.py").read_text(encoding="utf-8")
    assert '"optimization_performed": False' in source
    assert "run_full_backtest" in source
    assert "hyperopt" not in source.lower()
