"""Open-risk monitor tests (#105): alerts only, risk measured from entry."""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

# risk_warden lives in ../warden relative to this tests dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "warden"))
import risk_warden  # noqa: E402


def _conn(rows):
    """In-memory positions table; rows = (ft_trade_id, pair, sl_abs, state)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE positions (pos_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ft_trade_id INTEGER, pair TEXT, sl_abs REAL, state TEXT, open_date TEXT)")
    for i, (tid, pair, sl, state) in enumerate(rows):
        conn.execute(
            "INSERT INTO positions (ft_trade_id, pair, sl_abs, state, open_date) "
            "VALUES (?,?,?,?,?)", (tid, pair, sl, state, f"2026-10-0{i+1}T00:00:00+00:00"))
    return conn


def _cfg(tmp_path, monkeypatch, risk="12", max_open="5"):
    monkeypatch.setenv("KILLERS_RISK_USD", risk)
    monkeypatch.setenv("KILLERS_MAX_OPEN", max_open)
    monkeypatch.setenv("WARDEN_STATE", str(tmp_path / "state.json"))
    monkeypatch.delenv("WARDEN_RISK_CAP_USD", raising=False)
    return risk_warden.WardenConfig()


def _trade(tid, pair, amount, open_rate, current_rate=None, is_short=False):
    return {"trade_id": tid, "pair": pair, "amount": amount, "open_rate": open_rate,
            "current_rate": current_rate or open_rate, "is_short": is_short}


def _run(cfg, conn, trades):
    sent = []
    with patch.object(risk_warden, "get_open_trades", return_value=trades), \
         patch.object(risk_warden, "notify", side_effect=lambda c, text: sent.append(text)):
        summary = risk_warden.run_once(cfg, conn)
    return summary, sent


def test_has_no_write_path_to_freqtrade():
    assert not hasattr(risk_warden, "forceexit_full")
    assert not hasattr(risk_warden, "http_post_json")


def test_default_caps_follow_the_sizing_contract(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    assert cfg.cap_usd == 5 * 12 * 1.25
    assert cfg.trade_cap_usd == 12 * 1.5
    monkeypatch.setenv("WARDEN_RISK_CAP_USD", "40")
    assert risk_warden.WardenConfig().cap_usd == 40


def test_risk_is_measured_from_entry_not_the_mark():
    # A winner far above entry: its open profit is not counted as risk.
    winner = _trade(1, "DOT/USDC:USDC", 100, open_rate=0.84, current_rate=1.13)
    assert abs(risk_warden.risk_from_entry(winner, 0.75) - 9.0) < 1e-9
    short = _trade(2, "X/USDC:USDC", 2, open_rate=100, is_short=True)
    assert risk_warden.risk_from_entry(short, 120.0) == 40.0
    # Stop moved past entry: no capital at risk.
    assert risk_warden.risk_from_entry(_trade(3, "Y/USDC:USDC", 10, 100), 101.0) == 0.0


def test_within_contract_is_ok_and_silent(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    conn = _conn([(1, "A/USDC:USDC", 90.0, "open"), (2, "B/USDC:USDC", 45.0, "open")])
    trades = [_trade(1, "A/USDC:USDC", 1.2, 100), _trade(2, "B/USDC:USDC", 0.2, 100)]
    summary, sent = _run(cfg, conn, trades)
    assert summary["findings"] == []
    assert summary["total_risk"] == 12.0 + 11.0
    assert sent == []


def test_breach_alerts_once_then_resolves(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    rows = [(i, f"P{i}/USDC:USDC", 88.0, "open") for i in range(1, 8)]
    conn = _conn(rows)
    seven = [_trade(i, f"P{i}/USDC:USDC", 1.0, 100) for i in range(1, 8)]  # 7 x $12 = $84 > $75
    summary, sent = _run(cfg, conn, seven)
    assert summary["findings"] == ["total_over_cap"] and len(sent) == 1
    _, sent = _run(cfg, conn, seven)
    assert sent == []  # standing condition: no repeat alert
    _, sent = _run(cfg, conn, seven[:4])
    assert len(sent) == 1 and "resolved" in sent[0]


def test_oversized_trade_and_unmatched_rows_are_findings(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    conn = _conn([(1, "BIG/USDC:USDC", 80.0, "open"), (2, "OLD/USDT:USDT", 1.0, "closed")])
    trades = [_trade(1, "BIG/USDC:USDC", 1.0, 100),   # $20 > 1.5 x $12
              _trade(2, "NEW/USDC:USDC", 1.0, 100),   # ft_trade_id reuse
              _trade(3, "NOROW/USDC:USDC", 1.0, 100)]
    summary, sent = _run(cfg, conn, trades)
    assert summary["findings"] == [
        "trade_over_cap:1:BIG/USDC:USDC",
        "unmatched:2:NEW/USDC:USDC:pair_mismatch",
        "unmatched:3:NOROW/USDC:USDC:no_row",
    ]
    assert summary["total_risk"] == 20.0  # unmatched trades are never priced
    assert len(sent) == 1


def test_freqtrade_unreachable_is_a_finding(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    summary, sent = _run(cfg, _conn([]), None)
    assert summary["findings"] == ["ft_unreachable"]
    assert len(sent) == 1
