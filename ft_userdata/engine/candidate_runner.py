#!/usr/bin/env python3
"""Automated Quant Finance runner for explicit research candidates.

This runner is intentionally separate from live lifecycle. It registers a
candidate only in the current Python process, enables the data contract needed
by that candidate, runs deterministic validation/backtests, and writes an
immutable-style research artifact. It never changes bots_config or starts a bot.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config_builder, registry
from .calibration import _load_backtest_trades
from .data import validate_data
from .fixed_validation import _parse_closed_timerange, run_fixed_parameter_validation
from .monte_carlo import run_robustness_stage
from .viability import run_viability_stage

FT_DIR = registry.FT_DIR
USER_DATA = FT_DIR / "user_data"
CANDIDATE_DIR = FT_DIR / "research_candidates"
RESULTS_DIR = FT_DIR / "engine_results"

ORDERFLOW_CONFIG = {
    "cache_size": 1000,
    "max_candles": 1500,
    "scale": 0.5,
    "stacked_imbalance_range": 3,
    "imbalance_volume": 1,
    "imbalance_ratio": 3,
}


def _load_family(name: str) -> dict:
    path = CANDIDATE_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"candidate family manifest not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("status") != "research_only" or data.get("runtime_authority") is not False:
        raise ValueError("candidate manifest must be research_only with runtime_authority=false")
    return data


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


def enable_orderflow_config_in_memory() -> None:
    """Patch generated research configs only for this process."""
    config_builder.BASE_CONFIG["exchange"]["use_public_trades"] = True
    config_builder.BASE_CONFIG["orderflow"] = copy.deepcopy(ORDERFLOW_CONFIG)


def _docker_download(manifest: dict, pairs: list[str], timerange: str) -> dict:
    """Download 1m/5m candles and the raw public trades required by orderflow."""
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


def _trade_file_for_pair(pair: str) -> Path | None:
    data_dir = USER_DATA / "data" / "binance" / "futures"
    safe = pair.replace("/", "_").replace(":", "_")
    candidates = [
        data_dir / f"{safe}-trades.feather",
        data_dir / f"{safe.replace('_USDT_USDT', '_USDT')}-trades.feather",
    ]
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            return path
    # Keep this fail-closed while tolerating a future naming tweak.
    token = safe.split("_")[0]
    matches = sorted(data_dir.glob(f"{token}*trades.feather")) if data_dir.is_dir() else []
    return matches[0] if len(matches) == 1 and matches[0].stat().st_size > 0 else None


def _validate_data_contract(manifest: dict, pairs: list[str], timerange: str) -> dict:
    tfs = [manifest["market"]["timeframe"], manifest["market"]["detail_timeframe"]]
    candle_report = validate_data(pairs, tfs, timerange, trading_mode="futures")
    trade_files = {}
    missing = []
    for pair in pairs:
        path = _trade_file_for_pair(pair)
        if path is None:
            missing.append(pair)
        else:
            trade_files[pair] = str(path)
    if not candle_report.get("valid") or missing:
        raise RuntimeError(
            f"candidate data contract failed: candles_valid={candle_report.get('valid')} "
            f"missing_raw_trades={missing}"
        )
    return {"candles": candle_report, "raw_trade_files": trade_files}


def _snapshot_hash(paths: list[Path]) -> str:
    records = []
    for path in sorted(paths, key=lambda p: str(p)):
        records.append({"path": str(path), "sha256": _sha256_file(path), "bytes": path.stat().st_size})
    return _sha256_bytes(json.dumps(records, sort_keys=True, separators=(",", ":")).encode())


def _extract_viability_trades(via: dict) -> list[dict]:
    metrics = via.get("metrics") or {}
    result_file = metrics.get("_result_file")
    strategy_key = metrics.get("_bt_strategy")
    if not result_file or not strategy_key or not Path(result_file).is_file():
        return []
    return _load_backtest_trades(result_file, strategy_key)


def run_candidate(
    strategy_name: str,
    manifest: dict,
    pairs: list[str],
    timerange: str,
    mode: str,
) -> dict:
    register_candidate_in_memory(strategy_name, manifest)
    enable_orderflow_config_in_memory()

    viability = run_viability_stage(strategy_name, pairs, timerange)
    if viability.get("classification") == "DEAD":
        return {
            "strategy": strategy_name,
            "viability": viability,
            "temporal_validation": {"skipped": True, "reason": "DEAD in viability"},
            "robustness": {"skipped": True, "reason": "DEAD in viability"},
        }

    windows = int(registry.get_mode(mode).get("wf_windows", 6))
    temporal = run_fixed_parameter_validation(strategy_name, pairs, timerange, windows=max(3, windows))
    trades = _extract_viability_trades(viability)

    # V0 has frozen signal/exit parameters. Parameter perturbation would answer
    # a different experiment, so robustness here is trade-path MC only.
    robust_mode = dict(registry.get_mode(mode))
    robust_mode["perturb_pcts"] = []
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
    return {
        "strategy": strategy_name,
        "viability": viability,
        "temporal_validation": temporal,
        "robustness": robustness,
    }


def run_family(family: str, timerange: str, mode: str, strategy: str | None, download: bool) -> dict:
    _parse_closed_timerange(timerange)
    manifest = _load_family(family)
    pairs = list(manifest["development_universe"]["pairs"])
    selected = [strategy] if strategy else list(manifest["candidates"])
    unknown = sorted(set(selected) - set(manifest["candidates"]))
    if unknown:
        raise ValueError(f"strategies not in family: {unknown}")

    enable_orderflow_config_in_memory()
    download_result = _docker_download(manifest, pairs, timerange) if download else {"skipped": True}
    data_contract = _validate_data_contract(manifest, pairs, timerange)

    manifest_path = CANDIDATE_DIR / f"{family}.json"
    strategy_files = [USER_DATA / "strategies" / f"{name}.py" for name in selected]
    raw_trade_files = [Path(p) for p in data_contract["raw_trade_files"].values()]

    provenance = {
        "family": family,
        "timerange": timerange,
        "pairlist": pairs,
        "pairlist_sha256": _pairlist_hash(pairs),
        "manifest_sha256": _sha256_file(manifest_path),
        "strategy_sha256": {path.stem: _sha256_file(path) for path in strategy_files},
        "raw_trade_snapshot_sha256": _snapshot_hash(raw_trade_files),
        "freqtrade_image": manifest["market"]["image"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_authority": False,
    }

    candidate_results = {}
    for name in selected:
        candidate_results[name] = run_candidate(name, manifest, pairs, timerange, mode)

    result = {
        "schema_version": 1,
        "kind": "candidate_quant_finance_run",
        "provenance": provenance,
        "data_download": download_result,
        "data_contract": data_contract,
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
    parser.add_argument("--timerange", required=True, help="Closed YYYYMMDD-YYYYMMDD decision/development range")
    parser.add_argument("--mode", choices=list(registry.MODES.keys()), default="rigorous")
    parser.add_argument("--skip-download", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run_family(
            family=args.family,
            timerange=args.timerange,
            mode=args.mode,
            strategy=args.strategy,
            download=not args.skip_download,
        )
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
