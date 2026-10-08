#!/usr/bin/env python3
"""Top-level Quant Finance pipeline for research-only strategy candidates.

`candidate_runner` performs the expensive validation stages. This module adds
the decision-report layer promised by the candidate manifest: an explicit cost
contract, frozen-snapshot coverage checks, and per-asset/side diagnostics from
the exact exported trades. It never changes a strategy or authorizes capital.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import config_builder, registry
from .calibration import _load_backtest_trades
from .candidate_runner import run_family
from .data_snapshot_validation import require_snapshot_coverage
from .trade_diagnostics import summarize_trades


def _manifest_path(family: str) -> Path:
    return registry.FT_DIR / "research_candidates" / f"{family}.json"


def _load_manifest(family: str) -> dict:
    path = _manifest_path(family)
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _apply_explicit_cost_contract(manifest: dict) -> dict:
    """Pin the per-side fee in generated Freqtrade research configs.

    Relying on whatever fee metadata CCXT returns in a future run would make a
    historical decision artifact non-reproducible. V0 therefore declares a
    conservative market/taker fee explicitly in its immutable manifest.
    """
    contract = manifest.get("cost_contract") or {}
    fee = contract.get("fee_ratio_per_side")
    if fee is None:
        raise ValueError("candidate manifest must declare cost_contract.fee_ratio_per_side")
    fee = float(fee)
    if not (0.0 < fee < 0.01):
        raise ValueError(f"invalid candidate per-side fee ratio: {fee}")
    config_builder.BASE_CONFIG["fee"] = fee
    return contract


def _exported_trades(candidate: dict) -> list[dict]:
    metrics = ((candidate.get("viability") or {}).get("metrics") or {})
    result_file = metrics.get("_result_file")
    strategy_key = metrics.get("_bt_strategy")
    if not result_file or not strategy_key:
        return []
    path = Path(result_file)
    if not path.is_file():
        return []
    return _load_backtest_trades(path, strategy_key)


def _candidate_summary(name: str, candidate: dict) -> dict:
    viability = candidate.get("viability") or {}
    metrics = viability.get("metrics") or {}
    diagnostics = candidate.get("trade_diagnostics") or {}
    long_short = diagnostics.get("long_short_split") or {}
    worst = diagnostics.get("worst_asset") or {}
    turnover = diagnostics.get("turnover") or {}
    tim = diagnostics.get("time_in_market") or {}
    phase_a = candidate.get("phase_a_diagnostics") or {}
    null = phase_a.get("matched_random_null") or {}
    friction = phase_a.get("additional_friction") or {}
    return {
        "strategy": name,
        "quant_screen_passed": bool((candidate.get("quant_screen") or {}).get("passed", False)),
        "viability": viability.get("classification"),
        "total_trades": diagnostics.get("total_trades", metrics.get("total_trades")),
        "assets_traded": diagnostics.get("assets_traded"),
        "trades_by_asset": diagnostics.get("per_asset"),
        "long_trades": long_short.get("long_trades"),
        "short_trades": long_short.get("short_trades"),
        "long_sum_profit_ratio": long_short.get("long_sum_profit_ratio"),
        "short_sum_profit_ratio": long_short.get("short_sum_profit_ratio"),
        "net_profit": metrics.get("total_profit"),
        "profit_factor": metrics.get("profit_factor"),
        "sharpe": metrics.get("sharpe"),
        "sortino": metrics.get("sortino"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "worst_asset": worst.get("pair"),
        "worst_asset_sum_profit_ratio": worst.get("sum_profit_ratio"),
        "top_positive_pnl_concentration": diagnostics.get("top_positive_pnl_concentration"),
        "turnover_usdt": turnover.get("gross_entry_exit_notional_usdt"),
        "time_in_market_union_fraction": tim.get("union_fraction"),
        "gross_position_hours": tim.get("gross_position_hours"),
        "exit_reasons": diagnostics.get("exit_reasons"),
        "duration_minutes": diagnostics.get("duration_minutes"),
        "random_null_percentile": null.get("percentile_vs_null"),
        "random_null_upper_tail_p": null.get("upper_tail_p_value"),
        "additional_cost_break_even_bps": friction.get("additional_round_trip_break_even_bps"),
        "chronological_validation": candidate.get("temporal_validation"),
        "monte_carlo": (candidate.get("robustness") or {}).get("monte_carlo"),
    }


def run_quant_finance(
    family: str,
    timerange: str,
    mode: str = "rigorous",
    strategy: str | None = None,
    download: bool = True,
) -> dict:
    """Run candidates and persist a complete decision report.

    The returned report is evidence for research triage only. Even a fully
    passing report has `promotion_authority=false` and cannot alter runtime.
    """
    manifest = _load_manifest(family)
    cost_contract = _apply_explicit_cost_contract(manifest)
    result = run_family(family, timerange, mode, strategy, download)

    market = manifest["market"]
    data_coverage = require_snapshot_coverage(
        result["data_contract"],
        timerange,
        [market["timeframe"], market["detail_timeframe"]],
    )
    result["data_coverage"] = data_coverage

    for name, candidate in result["candidates"].items():
        trades = _exported_trades(candidate)
        candidate["trade_diagnostics"] = summarize_trades(trades, timerange)

    result["decision_report"] = {
        "kind": "quant_finance_candidate_decision_report",
        "timerange": timerange,
        "family": family,
        "cost_contract": cost_contract,
        "data_coverage_passed": data_coverage["passed"],
        "strategies": [
            _candidate_summary(name, candidate)
            for name, candidate in result["candidates"].items()
        ],
        "interpretation": (
            "A passing research screen is not promotion. Untouched holdout, "
            "portfolio admission and immutable forward evidence remain separate gates."
        ),
        "promotion_authority": False,
    }
    result["promotion_authority"] = False

    output = Path(result["result_file"])
    output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Automated Quant Finance pipeline for research-only strategy candidates"
    )
    parser.add_argument("--family", default="orderflow_auction_v0")
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--timerange", required=True, help="Closed YYYYMMDD-YYYYMMDD development range")
    parser.add_argument("--mode", choices=list(registry.MODES.keys()), default="rigorous")
    parser.add_argument("--skip-download", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run_quant_finance(
            family=args.family,
            timerange=args.timerange,
            mode=args.mode,
            strategy=args.strategy,
            download=not args.skip_download,
        )
    except Exception as exc:
        print(f"quant candidate pipeline failed: {exc}")
        return 2

    print(json.dumps({
        "result_file": result["result_file"],
        "strategies": list(result["candidates"]),
        "promotion_authority": False,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
