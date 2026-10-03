#!/usr/bin/env python3
"""Summarize the pre-declared OITrend backtest (README) from a Freqtrade
export zip/json. Stdlib only.

    python3 summarize_backtest.py backtest-result-*.zip > summary.json
"""
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path

SPLIT = "2026-09-17"
SLIP_BPS_PER_FILL = 5.0


def load(path):
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.endswith(".json") and "_config" not in n
                        and "market_change" not in n)
            return json.loads(z.read(name))
    return json.loads(path.read_text())


def metrics(trades, slip_bps=0.0):
    pnl = [t["profit_abs"] - 2 * slip_bps / 1e4 * t["stake_amount"] for t in trades]
    wins = sum(p for p in pnl if p > 0)
    losses = -sum(p for p in pnl if p < 0)
    return {"trades": len(pnl), "net_usdt": round(sum(pnl), 4),
            "profit_factor": (round(wins / losses, 3) if losses else (None if not wins else float("inf"))),
            "win_rate": round(sum(p > 0 for p in pnl) / len(pnl), 3) if pnl else None}


def summarize(result):
    out = {}
    for name, s in result["strategy"].items():
        trades = s["trades"]
        first = [t for t in trades if t["open_date"][:10] < SPLIT]
        second = [t for t in trades if t["open_date"][:10] >= SPLIT]
        out[name] = {"full": metrics(trades), "full_slip": metrics(trades, SLIP_BPS_PER_FILL),
                     "half1": metrics(first), "half2": metrics(second),
                     "exit_reasons": dict(Counter(t["exit_reason"] for t in trades))}
    return out


def pctile(values, x):
    v = [a for a in values if a is not None]
    return sum(a <= x for a in v) / len(v) if v and x is not None else None


def reading_rule(summary, gate="OITrendPullbackV1CausalOI", control="OITrendPullbackV1PriceOnly",
                 placebo_prefix="OITrendPullbackV1Placebo"):
    placebos = {k: v for k, v in summary.items() if k.startswith(placebo_prefix)}
    verdict = {}
    for part in ("full", "half1", "half2"):
        g, c = summary[gate][part], summary[control][part]
        row = {}
        for m in ("profit_factor", "net_usdt"):
            pv = sorted(p[part][m] for p in placebos.values() if p[part][m] is not None)
            p90 = pv[int(0.9 * (len(pv) - 1))] if pv else None
            row[m] = {"gate": g[m], "price_only": c[m],
                      "placebo_median": pv[len(pv) // 2] if pv else None, "placebo_p90": p90,
                      "gate_percentile_in_placebo": pctile(pv, g[m]),
                      "beats_control_and_p90": (g[m] is not None and c[m] is not None and p90 is not None
                                                and g[m] > c[m] and g[m] > p90)}
        verdict[part] = row
    shown = all(verdict[p][m]["beats_control_and_p90"] for p in ("half1", "half2")
                for m in ("profit_factor", "net_usdt"))
    return {"by_part": verdict, "discriminative_power": "demonstrated" if shown else "not demonstrated"}


def strict_json(obj):
    """Infinite profit factors (no losing trade) become the string "inf"."""
    if isinstance(obj, dict):
        return {k: strict_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strict_json(v) for v in obj]
    if isinstance(obj, float) and obj == float("inf"):
        return "inf"
    return obj


if __name__ == "__main__":
    s = summarize(load(sys.argv[1]))
    print(json.dumps(strict_json({"variants": s, "reading_rule": reading_rule(s)}), indent=2, allow_nan=False))
