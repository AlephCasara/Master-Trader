"""Deterministic tests for candidate decision-report diagnostics."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent
MODULE = ROOT / "ft_userdata" / "engine" / "trade_diagnostics.py"
PIPELINE = ROOT / "ft_userdata" / "engine" / "quant_candidate_pipeline.py"


def _module():
    spec = importlib.util.spec_from_file_location("candidate_trade_diagnostics", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _trade(pair, is_short, profit_ratio, profit_abs, opened, closed, stake=100.0, leverage=1.0, reason="phase_a_60m"):
    return {
        "pair": pair,
        "is_short": is_short,
        "profit_ratio": profit_ratio,
        "profit_abs": profit_abs,
        "open_date": opened,
        "close_date": closed,
        "stake_amount": stake,
        "leverage": leverage,
        "exit_reason": reason,
    }


def test_trade_diagnostics_report_asset_side_turnover_and_time_in_market():
    mod = _module()
    trades = [
        _trade("BTC/USDT:USDT", False, 0.02, 2.0, "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        _trade("ETH/USDT:USDT", True, -0.01, -1.0, "2026-01-01T00:30:00Z", "2026-01-01T01:30:00Z", reason="stop_loss"),
        _trade("BTC/USDT:USDT", True, 0.01, 1.0, "2026-01-02T00:00:00Z", "2026-01-02T00:30:00Z", leverage=2.0),
    ]
    out = mod.summarize_trades(trades, "20260101-20260103")

    assert out["measured"] is True
    assert out["total_trades"] == 3
    assert out["assets_traded"] == 2
    assert out["long_short_split"]["long_trades"] == 1
    assert out["long_short_split"]["short_trades"] == 2
    assert out["worst_asset"]["pair"] == "ETH/USDT:USDT"
    # 100*2 for each 1x trade + 100*2*2 for the 2x trade.
    assert out["turnover"]["gross_entry_exit_notional_usdt"] == 800.0
    # Intervals 00:00-01:30 plus next-day 00:00-00:30 = 2h union / 48h.
    assert abs(out["time_in_market"]["union_fraction"] - (2.0 / 48.0)) < 1e-12
    assert out["exit_reasons"] == {"phase_a_60m": 2, "stop_loss": 1}


def test_trade_diagnostics_empty_sample_is_fail_closed_measurement():
    mod = _module()
    out = mod.summarize_trades([], "20260101-20260103")
    assert out == {
        "measured": False,
        "reason": "no exported trades",
        "total_trades": 0,
        "assets_traded": 0,
    }


def test_top_level_quant_pipeline_enriches_and_persists_without_promotion_authority():
    source = PIPELINE.read_text(encoding="utf-8")
    assert "run_family(" in source
    assert "summarize_trades" in source
    assert 'candidate["trade_diagnostics"]' in source
    assert 'result["decision_report"]' in source
    assert '"promotion_authority": False' in source
    assert "bots_config" not in source
    assert "forceenter" not in source.lower()
    assert "forceexit" not in source.lower()
