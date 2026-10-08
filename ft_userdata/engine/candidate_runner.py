#!/usr/bin/env python3
"""Automated Quant Finance runner for explicit research candidates.

Candidates are registered only in this Python process, never as active bots.
The runner freezes assets/data/code, enables the candidate data contract, runs
existing Engine-v2 validation, and applies preregistered Phase-A diagnostics.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config_builder, registry
from .calibration import _load_backtest_trades
from .data import validate_data
from .fixed_validation import _parse_closed_timerange, run_fixed_parameter_validation
from .monte_carlo import run_robustness_stage
from .phase_a_diagnostics import additional_friction_stress, matched_random_null
from .viability import run_viability_stage

FT_DIR = registry.FT_DIR
USER_DATA = FT_DIR / "user_data"
CANDIDATE_DIR = FT_DIR / "research_candidates"
RESULTS_DIR = FT_DIR / "engine_results"
MAX_ORDERFLOW_CANDLES_PER_PAIR = 120_000

ORDERFLOW_CONFIG = {
    "cache_size": 1000,
    "max_candles": 1500,
    "scale": 0.5,
    "stacked_imbalance_range": 3,
    "imbalance_volume": 1,
    "imbalance_ratio": 3,
}


def _load_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _load_family(name: str) -> dict:
    data = _load_json(CANDIDATE_DIR / f"{name}.json")
    if data.get("status") != "research_only" or data.get("runtime_authority") is not False:
        raise ValueError("candidate manifest must be research_only with runtime_authority=false")
    return data


def _load_policy(family: str) -> dict:
    policy = _load_json(CANDIDATE_DIR / f"{family}_policy.json")
    if policy.get("family") != family or policy.get("kind") != "research_screen_not_promotion":
        raise ValueError("candidate quant policy does not match family or authority boundary")
    return policy


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _pairlist_hash(pairs: list[str]) -> str:
    return _sha256_bytes(json.dumps(sorted(pairs), separators=(",", ":")).encode())


def _timeframe_minutes(timeframe: str) -> int:
    if timeframe.endswith("m"):
        return int(timeframe[:-1])
    if timeframe.endswith("h"):
        return int(timeframe[:-1]) * 60
    raise ValueError(f"candidate runner supports minute/hour timeframes, got {timeframe}")


def required_orderflow_candles(timerange: str, timeframe: str, startup: int = 128) -> int:
    start, end = _parse_closed_timerange(timerange)
    minutes = (end - start).total_seconds() / 60.0
    return int(math.ceil(minutes / _timeframe_minutes(timeframe))) + startup


def _candidate_metadata(manifest: dict) -> dict:
    market = manifest["market"]
    execution = manifest["execution_contract"]
    return {
        "timeframe": market["timeframe"],
        "trading_mode": market["trading_mode"],
        "margin_mode": market.get("margin_mode", "isolated"),
        "port": 0,
        "max_open_trades": int(execution["max_open_trades"]),
        "stake_amount": float(execution["stake_amount_usdt"]),
        "dry_run_wallet": 1000,
        "image": market["image"],
        "informative_tfs": [],
        "backtest_config": None,
        "status": "candidate",
        "research_only": True,
        "data_requirements": {
            "public_trades": True,
            "detail_timeframe": market["detail_timeframe"],
        },
    }


def register_candidate_in_memory(name: str, manifest: dict) -> None:
    """Register a candidate without making it active or mutating repo files."""
    if name not in manifest["candidates"]:
        raise ValueError(f"{name} is not declared by family {manifest['family']}")
    registry.STRATEGIES[name] = _candidate_metadata(manifest)
    assert name not in registry.get_active_strategies()


def enable_orderflow_config_in_memory(timerange: str, timeframe: str) -> int:
    """Cover the entire research range with orderflow or refuse to run."""
    candles = required_orderflow_candles(timerange, timeframe)
    if candles > MAX_ORDERFLOW_CANDLES_PER_PAIR:
        raise ValueError(
            f"orderflow range requires {candles:,} candles/pair, above the "
            f"research memory budget {MAX_ORDERFLOW_CANDLES_PER_PAIR:,}; "
            "use a shorter closed development range or implement chunked orderflow evaluation"
        )
    config_builder.BASE_CONFIG["exchange"]["use_public_trades"] = True
    orderflow = copy.deepcopy(ORDERFLOW_CONFIG)
    orderflow["cache_size"] = candles
    orderflow["max_candles"] = candles
    config_builder.BASE_CONFIG["orderflow"] = orderflow
    return candles


def _docker_download(manifest: dict, pairs: list[str], timerange: str) -> dict:
    """Download normal 1m/5m candles plus the raw public-trade archive."""
    image = manifest["market"]["image"]
    timeframes = sorted({manifest["market"]["timeframe"], manifest["market"]["detail_timeframe"]})
    mount = f"{USER_DATA}:/freqtrade/user_data"
    base = ["docker", "run", "--rm", "-v", mount, image, "download-data"]

    ohlcv_cmd = base + [
        "--exchange", manifest["market"]["exchange"],
        "--pairs", *pairs,
        "--timeframes", *timeframes,
        "--timerange", timerange,
        "--trading-mode", manifest["market"]["trading_mode"],
    ]
    trades_cmd = base + [
        "--exchange", manifest["market"]["exchange"],
        "--pairs", *pairs,
        "--timerange", timerange,
        "--trading-mode", manifest["market"]["trading_mode"],
        "--dl-trades",
        "--data-format-trades", "feather",
    ]

    result = {"ohlcv_command": ohlcv_cmd, "trades_command": trades_cmd}
    for label, command in (("ohlcv", ohlcv_cmd), ("trades", trades_cmd)):
        proc = subprocess.run(command, capture_output=True, text=True, timeout=3600)
        result[f"{label}_returncode"] = proc.returncode
        result[f"{label}_stderr_tail"] = (proc.stderr or "")[-2000:]
        if proc.returncode != 0:
            raise RuntimeError(f"{label} data download failed: {(proc.stderr or proc.stdout)[-2000:]}")
    return result


def _data_dir() -> Path:
    return USER_DATA / "data" / "binance" / "futures"


def _ohlcv_file(pair: str, timeframe: str) -> Path:
    safe = pair.replace("/", "_").replace(":", "_")
    return _data_dir() / f"{safe}-{timeframe}-futures.feather"


def _trade_file_for_pair(pair: str) -> Path | None:
    data_dir = _data_dir()
    safe = pair.replace("/", "_").replace(":", "_")
    candidates = [
        data_dir / f"{safe}-trades.feather",
        data_dir / f"{safe.replace('_USDT_USDT', '_USDT')}-trades.feather",
    ]
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            return path
    token = safe.split("_")[0]
    matches = sorted(data_dir.glob(f"{token}*trades.feather")) if data_dir.is_dir() else []
    return matches[0] if len(matches) == 1 and matches[0].stat().st_size > 0 else None


def _validate_data_contract(manifest: dict, pairs: list[str], timerange: str) -> dict:
    tfs = [manifest["market"]["timeframe"], manifest["market"]["detail_timeframe"]]
    candle_report = validate_data(pairs, tfs, timerange, trading_mode="futures")
    trade_files: dict[str, str] = {}
    ohlcv_files: dict[str, list[str]] = {}
    missing_trades = []
    missing_ohlcv = []

    for pair in pairs:
        trade_path = _trade_file_for_pair(pair)
        if trade_path is None:
            missing_trades.append(pair)
        else:
            trade_files[pair] = str(trade_path)

        pair_candles = []
        for tf in tfs:
            path = _ohlcv_file(pair, tf)
            if not path.is_file() or path.stat().st_size == 0:
                missing_ohlcv.append(f"{pair}@{tf}")
            else:
                pair_candles.append(str(path))
        ohlcv_files[pair] = pair_candles

    if not candle_report.get("valid") or missing_trades or missing_ohlcv:
        raise RuntimeError(
            f"candidate data contract failed: candles_valid={candle_report.get('valid')} "
            f"missing_raw_trades={missing_trades} missing_ohlcv={missing_ohlcv}"
        )
    return {
        "candles": candle_report,
        "raw_trade_files": trade_files,
        "ohlcv_files": ohlcv_files,
    }


def _snapshot_hash(paths: list[Path]) -> str:
    records = []
    for path in sorted(set(paths), key=lambda p: str(p)):
        records.append({"path": str(path), "sha256": _sha256_file(path), "bytes": path.stat().st_size})
    return _sha256_bytes(json.dumps(records, sort_keys=True, separators=(",", ":")).encode())


def _extract_viability_trades(via: dict) -> list[dict]:
    metrics = via.get("metrics") or {}
    result_file = metrics.get("_result_file")
    strategy_key = metrics.get("_bt_strategy")
    if not result_file or not strategy_key or not Path(result_file).is_file():
        return []
    return _load_backtest_trades(result_file, strategy_key)


def _quant_screen(
    policy: dict,
    viability: dict,
    temporal: dict,
    robustness: dict,
    phase_a: dict,
) -> dict:
    """Apply the preregistered research screen. Passing never authorizes live."""
    metrics = viability.get("metrics") or {}
    lookahead = viability.get("lookahead") or {}
    null = phase_a.get("matched_random_null") or {}
    friction = phase_a.get("additional_friction") or {}

    measured = int(temporal.get("windows_measured") or 0)
    requested = int(temporal.get("windows_requested") or 0)
    profitable = int(temporal.get("profitable_windows") or 0)
    profitable_fraction = profitable / measured if measured else 0.0
    worst_dd = temporal.get("worst_drawdown_pct")

    checks = {
        "viability": {
            "observed": viability.get("classification"),
            "required": "VIABLE",
            "passed": viability.get("classification") == "VIABLE",
        },
        "lookahead": {
            "observed": lookahead.get("passed"),
            "required": True,
            "passed": lookahead.get("passed") is True,
        },
        "sample_floor": {
            "observed": int(metrics.get("total_trades") or 0),
            "required_min": int(policy["sample_floor"]["min_total_trades"]),
            "passed": int(metrics.get("total_trades") or 0) >= int(policy["sample_floor"]["min_total_trades"]),
        },
        "chronological_coverage": {
            "observed": f"{measured}/{requested}",
            "required": "all windows measured",
            "passed": requested > 0 and measured == requested,
        },
        "chronological_profitability": {
            "observed": profitable_fraction,
            "required_min": float(policy["temporal_stability"]["min_profitable_window_fraction"]),
            "passed": profitable_fraction >= float(policy["temporal_stability"]["min_profitable_window_fraction"]),
        },
        "chronological_drawdown": {
            "observed": worst_dd,
            "required_max": float(policy["temporal_stability"]["max_worst_window_drawdown_pct"]),
            "passed": worst_dd is not None and float(worst_dd) <= float(policy["temporal_stability"]["max_worst_window_drawdown_pct"]),
        },
        "monte_carlo": {
            "observed": robustness.get("combined_verdict"),
            "allowed": list(policy["robustness"]["allowed_engine_verdicts"]),
            "passed": robustness.get("combined_verdict") in policy["robustness"]["allowed_engine_verdicts"],
        },
        "matched_random_null": {
            "observed_p": null.get("upper_tail_p_value"),
            "required_max_p": float(policy["multiple_testing"]["max_candidate_random_null_p_value"]),
            "passed": null.get("measured") is True
            and float(null.get("upper_tail_p_value", 1.0)) <= float(policy["multiple_testing"]["max_candidate_random_null_p_value"]),
        },
        "additional_friction": {
            "observed_break_even_bps": friction.get("additional_round_trip_break_even_bps"),
            "required_min_bps": float(policy["execution_friction"]["min_additional_round_trip_break_even_bps"]),
            "passed": friction.get("measured") is True
            and float(friction.get("additional_round_trip_break_even_bps", 0.0)) >= float(policy["execution_friction"]["min_additional_round_trip_break_even_bps"]),
        },
    }
    passed = all(item["passed"] for item in checks.values())
    return {
        "kind": "research_screen_not_promotion",
        "passed": passed,
        "checks": checks,
        "next_gate": "freeze candidate -> untouched holdout / portfolio admission / forward epoch" if passed else "falsify or register a new experiment",
        "promotion_authority": False,
    }


def run_candidate(
    strategy_name: str,
    manifest: dict,
    policy: dict,
    pairs: list[str],
    pair_ohlcv_files: dict[str, str],
    timerange: str,
    mode: str,
) -> dict:
    register_candidate_in_memory(strategy_name, manifest)
    viability = run_viability_stage(strategy_name, pairs, timerange)
    trades = _extract_viability_trades(viability)
    phase_a = {
        "additional_friction": additional_friction_stress(trades),
        "matched_random_null": matched_random_null(
            trades=trades,
            pair_ohlcv_files=pair_ohlcv_files,
            timerange=timerange,
            iterations=1000,
        ),
    }

    if viability.get("classification") == "DEAD":
        return {
            "strategy": strategy_name,
            "viability": viability,
            "phase_a_diagnostics": phase_a,
            "temporal_validation": {"skipped": True, "reason": "DEAD in viability"},
            "robustness": {"skipped": True, "reason": "DEAD in viability"},
            "quant_screen": {
                "kind": "research_screen_not_promotion",
                "passed": False,
                "reason": "DEAD in viability",
                "promotion_authority": False,
            },
        }

    windows = int(registry.get_mode(mode).get("wf_windows", 6))
    temporal = run_fixed_parameter_validation(strategy_name, pairs, timerange, windows=max(3, windows))

    robust_mode = dict(registry.get_mode(mode))
    robust_mode["perturb_pcts"] = []  # signal + Phase-A exits are frozen in V0
    if robust_mode.get("mc_iterations", 0) == 0:
        robust_mode["mc_iterations"] = 500
    robustness = run_robustness_stage(
        strategy_name=strategy_name,
        trades=trades,
        base_params={},
        pairs=pairs,
        timerange=timerange,
        mode_config=robust_mode,
    )
    screen = _quant_screen(policy, viability, temporal, robustness, phase_a)
    return {
        "strategy": strategy_name,
        "viability": viability,
        "phase_a_diagnostics": phase_a,
        "temporal_validation": temporal,
        "robustness": robustness,
        "quant_screen": screen,
    }


def run_family(family: str, timerange: str, mode: str, strategy: str | None, download: bool) -> dict:
    _parse_closed_timerange(timerange)
    manifest = _load_family(family)
    policy = _load_policy(family)
    pairs = list(manifest["development_universe"]["pairs"])
    selected = [strategy] if strategy else list(manifest["candidates"])
    unknown = sorted(set(selected) - set(manifest["candidates"]))
    if unknown:
        raise ValueError(f"strategies not in family: {unknown}")

    orderflow_candles = enable_orderflow_config_in_memory(timerange, manifest["market"]["timeframe"])
    download_result = _docker_download(manifest, pairs, timerange) if download else {"skipped": True}
    data_contract = _validate_data_contract(manifest, pairs, timerange)

    manifest_path = CANDIDATE_DIR / f"{family}.json"
    policy_path = CANDIDATE_DIR / f"{family}_policy.json"
    strategy_files = [USER_DATA / "strategies" / f"{name}.py" for name in selected]
    shared_files = [
        USER_DATA / "strategies" / "orderflow_auction_common.py",
        USER_DATA / "strategies" / "orderflow_auction_base.py",
    ]
    raw_trade_files = [Path(p) for p in data_contract["raw_trade_files"].values()]
    ohlcv_files = [Path(p) for paths in data_contract["ohlcv_files"].values() for p in paths]

    primary_tf = manifest["market"]["timeframe"]
    pair_ohlcv_files = {
        pair: next(path for path in paths if f"-{primary_tf}-" in path)
        for pair, paths in data_contract["ohlcv_files"].items()
    }

    provenance = {
        "family": family,
        "timerange": timerange,
        "pairlist": pairs,
        "pairlist_sha256": _pairlist_hash(pairs),
        "manifest_sha256": _sha256_file(manifest_path),
        "quant_policy_sha256": _sha256_file(policy_path),
        "strategy_sha256": {path.stem: _sha256_file(path) for path in strategy_files + shared_files},
        "data_snapshot_sha256": _snapshot_hash(raw_trade_files + ohlcv_files),
        "freqtrade_image": manifest["market"]["image"],
        "orderflow_candles_per_pair": orderflow_candles,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_authority": False,
    }

    candidate_results = {
        name: run_candidate(name, manifest, policy, pairs, pair_ohlcv_files, timerange, mode)
        for name in selected
    }
    result = {
        "schema_version": 1,
        "kind": "candidate_quant_finance_run",
        "provenance": provenance,
        "data_download": download_result,
        "data_contract": data_contract,
        "quant_policy": policy,
        "candidates": candidate_results,
        "promotion_authority": False,
    }

    run_dir = RESULTS_DIR / f"candidate-{family}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    run_dir.mkdir(parents=True, exist_ok=False)
    out = run_dir / "candidate_results.json"
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    result["result_file"] = str(out)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run research-only candidates through Quant Finance")
    parser.add_argument("--family", default="orderflow_auction_v0")
    parser.add_argument("--strategy", default=None, help="Optional one candidate from the family")
    parser.add_argument("--timerange", required=True, help="Closed YYYYMMDD-YYYYMMDD development range")
    parser.add_argument("--mode", choices=list(registry.MODES.keys()), default="rigorous")
    parser.add_argument("--skip-download", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run_family(args.family, args.timerange, args.mode, args.strategy, not args.skip_download)
    except Exception as exc:
        print(f"candidate runner failed: {exc}")
        return 2
    print(json.dumps({
        "result_file": result["result_file"],
        "strategies": list(result["candidates"]),
        "promotion_authority": result["promotion_authority"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
