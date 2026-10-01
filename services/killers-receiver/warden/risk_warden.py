#!/usr/bin/env python3
"""Open-risk monitor for the Killers copy-trader (alerts only).

Has no write path to Freqtrade: it never places, closes or cancels anything.
Entry admission is the receiver's job (KILLERS_MAX_OPEN); this script checks
that the open book still matches the sizing contract and says so when not.

Runs inside the killers-receiver container (same image, env and DB volume),
one pass per invocation from host cron:

    docker exec killers-receiver python3 /app/warden/risk_warden.py

Each pass:

  1. GET {KILLERS_FT_BASE_URL}/api/v1/status for the open trades.
  2. Read each trade's posted stop (`sl_abs`) from the receiver DB
     (KILLERS_DB, opened read-only), matched by ft_trade_id AND pair. A row
     whose pair disagrees is reported, never priced off.
  3. Capital at risk per trade = loss from ENTRY to the posted stop on the
     remaining amount (long: max(0, open_rate - sl) * amount; short mirrored).
     Measured from entry, not the current mark: open profit on a running
     winner is not risk to cut, the exit ladder is meant to let it run.
  4. Findings: total risk over the cap (default KILLERS_MAX_OPEN x
     KILLERS_RISK_USD x 1.25); a single trade over 1.5 x KILLERS_RISK_USD;
     an open trade with no matched posted stop; Freqtrade or DB unreadable.
  5. Logs one line per pass. Sends a Telegram alert through KILLERS_NOTIFY_URL
     only when the set of findings changes (state file beside the DB), so a
     standing condition alerts once and again when it clears.

Env (all already set on the receiver container):
  KILLERS_FT_BASE_URL, KILLERS_FT_USERNAME, KILLERS_FT_PASSWORD
  KILLERS_DB                 receiver SQLite path
  KILLERS_RISK_USD, KILLERS_MAX_OPEN
  KILLERS_NOTIFY_URL, TRADE_WEBHOOK_NOTIFY_TOKEN (optional)
  WARDEN_RISK_CAP_USD        optional override of the total cap
  WARDEN_STATE               optional state path (default <db dir>/warden_state.json)
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

TOTAL_SLACK = 1.25
TRADE_SLACK = 1.5


class WardenConfig:
    def __init__(self):
        env = os.environ.get
        self.ft_base = env("KILLERS_FT_BASE_URL", "http://ft-killers-scalp:8080").rstrip("/")
        self.ft_user = env("KILLERS_FT_USERNAME", "")
        self.ft_pass = env("KILLERS_FT_PASSWORD", "")
        self.db_path = env("KILLERS_DB", "/var/lib/killers/receiver-hyperliquid.sqlite")
        self.risk_usd = float(env("KILLERS_RISK_USD", "2"))
        self.max_open = int(env("KILLERS_MAX_OPEN", "10"))
        override = env("WARDEN_RISK_CAP_USD", "").strip()
        self.cap_usd = (float(override) if override
                        else self.max_open * self.risk_usd * TOTAL_SLACK)
        self.trade_cap_usd = self.risk_usd * TRADE_SLACK
        self.notify_url = env("KILLERS_NOTIFY_URL", "http://trade-webhook:8088/test/notify")
        self.notify_token = env("TRADE_WEBHOOK_NOTIFY_TOKEN", "").strip()
        self.state_path = Path(env("WARDEN_STATE", "") or
                               Path(self.db_path).with_name("warden_state.json"))


def _log(msg: str) -> None:
    print(f"[warden] {msg}", flush=True)


def get_open_trades(cfg: WardenConfig) -> Optional[list[dict]]:
    """GET /api/v1/status → list of open trades, or None when unreadable."""
    token = base64.b64encode(f"{cfg.ft_user}:{cfg.ft_pass}".encode()).decode()
    req = urllib.request.Request(f"{cfg.ft_base}/api/v1/status",
                                 headers={"Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, ValueError, OSError) as e:
        _log(f"WARNING /status failed: {e}")
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("trades"), list):
        return data["trades"]
    return None


def notify(cfg: WardenConfig, text: str) -> None:
    """Best-effort Telegram alert via trade-webhook; never raises."""
    if not cfg.notify_url:
        return
    headers = {"Content-Type": "application/json"}
    if cfg.notify_token:
        headers["X-Notify-Token"] = cfg.notify_token
    req = urllib.request.Request(cfg.notify_url, data=json.dumps({"text": text}).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
    except (urllib.error.URLError, OSError) as e:
        _log(f"WARNING notify failed: {e}")


def load_sl_abs(conn: sqlite3.Connection, ft_trade_id, pair: str) -> tuple[Optional[float], str]:
    """(sl_abs, reason); reason ∈ {'matched','pair_mismatch','no_row','no_sl'}."""
    row = conn.execute(
        "SELECT sl_abs, pair FROM positions WHERE ft_trade_id = ? "
        "ORDER BY (state='open') DESC, open_date DESC LIMIT 1",
        (ft_trade_id,),
    ).fetchone()
    if row is None:
        return None, "no_row"
    if row["pair"] != pair:
        return None, "pair_mismatch"
    try:
        sl = float(row["sl_abs"])
    except (TypeError, ValueError):
        return None, "no_sl"
    return (sl, "matched") if sl > 0 else (None, "no_sl")


def risk_from_entry(trade: dict, sl_abs: float) -> float:
    """Loss from entry to the posted stop on the remaining amount, floored at 0."""
    try:
        amount = float(trade.get("amount") or 0.0)
        entry = float(trade.get("open_rate") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if amount <= 0 or entry <= 0:
        return 0.0
    move = sl_abs - entry if trade.get("is_short") else entry - sl_abs
    return max(0.0, move) * amount


def assess(cfg: WardenConfig, conn: sqlite3.Connection, trades: list[dict]) -> dict:
    """Pure evaluation of one snapshot → summary with sorted findings."""
    findings, per_trade, total = [], [], 0.0
    for t in trades:
        tid, pair = t.get("trade_id"), t.get("pair")
        sl_abs, reason = load_sl_abs(conn, tid, pair)
        if reason != "matched":
            findings.append(f"unmatched:{tid}:{pair}:{reason}")
            continue
        risk = risk_from_entry(t, sl_abs)
        per_trade.append({"trade_id": tid, "pair": pair, "risk": risk})
        total += risk
        if risk > cfg.trade_cap_usd:
            findings.append(f"trade_over_cap:{tid}:{pair}")
    if total > cfg.cap_usd:
        findings.append("total_over_cap")
    return {"total_risk": total, "cap_usd": cfg.cap_usd, "per_trade": per_trade,
            "open": len(trades), "findings": sorted(findings)}


def _load_state(path: Path) -> list:
    try:
        return json.loads(path.read_text()).get("findings", [])
    except (OSError, ValueError, AttributeError):
        return []


def _save_state(path: Path, findings: list) -> None:
    try:
        path.write_text(json.dumps({"findings": findings}))
    except OSError as e:
        _log(f"WARNING cannot write state {path}: {e}")


def _describe(summary: dict) -> str:
    worst = sorted(summary["per_trade"], key=lambda x: -x["risk"])[:3]
    top = ", ".join(f"{p['pair']} ${p['risk']:.2f}" for p in worst) or "none"
    return (f"open={summary['open']} risk=${summary['total_risk']:.2f} "
            f"cap=${summary['cap_usd']:.2f} largest: {top}")


def run_once(cfg: WardenConfig, conn: Optional[sqlite3.Connection] = None) -> dict:
    trades = get_open_trades(cfg)
    if trades is None:
        summary = {"total_risk": 0.0, "cap_usd": cfg.cap_usd, "per_trade": [],
                   "open": 0, "findings": ["ft_unreachable"]}
    else:
        own = conn is None
        try:
            if own:
                conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
            summary = assess(cfg, conn, trades)
        except sqlite3.Error as e:
            _log(f"WARNING receiver DB unreadable: {e}")
            summary = {"total_risk": 0.0, "cap_usd": cfg.cap_usd, "per_trade": [],
                       "open": len(trades), "findings": ["db_unreadable"]}
        finally:
            if own and conn is not None:
                conn.close()

    findings = summary["findings"]
    _log(("ALERT " + " ".join(findings) + " | " if findings else "OK ") + _describe(summary))
    previous = _load_state(cfg.state_path)
    if findings != previous:
        if findings:
            notify(cfg, "[killers-warden] " + ", ".join(findings) + " | " + _describe(summary))
        elif previous:
            notify(cfg, "[killers-warden] resolved | " + _describe(summary))
        _save_state(cfg.state_path, findings)
    summary["alerted"] = findings != previous
    return summary


def main():
    run_once(WardenConfig())
    return 0


if __name__ == "__main__":
    sys.exit(main())
