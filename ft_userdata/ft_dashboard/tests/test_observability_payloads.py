"""Observability payload regressions (bd 9km/y44/ggb).

recent_trades rows must carry the stake/fee fields the frontend needs for
per-trade cost detail (missing keys render as '—', never as a dropped row),
and /api/strategy_notes must degrade to {} instead of erroring when the
explainer file is absent or malformed — notes are served, never fabricated.
"""

import asyncio
import json

from fastapi.testclient import TestClient

import app


def _configure_poll(monkeypatch, closed_trades):
    async def fake_get(_client, _url, path):
        if path == "status":
            return [], None
        if path == "show_config":
            return {"dry_run": True, "timeframe": "1h"}, None
        if path == "whitelist":
            return {"whitelist": []}, None
        if path == "balance":
            return {"starting_capital": 100.0, "total_bot": 100.0}, None
        return {}, None  # profit

    async def fake_epoch_trades(_client, _bot, _epoch):
        return closed_trades, True, None

    monkeypatch.setattr(app, "_get", fake_get)
    monkeypatch.setattr(app, "_fetch_epoch_trades", fake_epoch_trades)
    monkeypatch.setattr(app, "_trade_cache", {})


def test_recent_trades_carry_stake_and_fee_fields(monkeypatch):
    bot = {
        "key": "obs-payload",
        "name": "ObsPayloadV1",
        "label": "obs-payload",
        "url": "http://ft-obs-payload:8080",
    }
    closed = [
        {
            "trade_id": 1, "pair": "BTC/USDT", "is_open": False,
            "close_timestamp": 2000, "profit_abs": 1.0, "profit_pct": 1.0,
            "stake_amount": 100.0, "fee_open_cost": 0.1, "fee_close_cost": 0.2,
        },
        # A payload lacking the fields must still produce a row with None
        # placeholders, not drop the trade.
        {
            "trade_id": 2, "pair": "ETH/USDT", "is_open": False,
            "close_timestamp": 1000, "profit_abs": -0.5, "profit_pct": -0.5,
        },
    ]
    _configure_poll(monkeypatch, closed)

    snapshot = asyncio.run(app._poll_bot(object(), bot))

    rows = snapshot["recent_trades"]
    assert [row["pair"] for row in rows] == ["BTC/USDT", "ETH/USDT"]
    assert rows[0]["stake_amount"] == 100.0
    assert rows[0]["fee_open_cost"] == 0.1
    assert rows[0]["fee_close_cost"] == 0.2
    assert rows[1]["stake_amount"] is None
    assert rows[1]["fee_open_cost"] is None
    assert rows[1]["fee_close_cost"] is None


def test_strategy_notes_empty_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "_STRATEGY_NOTES_PATH", tmp_path / "strategy_notes.json")
    assert TestClient(app.app).get("/api/strategy_notes").json() == {}


def test_strategy_notes_empty_on_invalid_json(monkeypatch, tmp_path):
    path = tmp_path / "strategy_notes.json"
    path.write_text("{not json")
    monkeypatch.setattr(app, "_STRATEGY_NOTES_PATH", path)
    assert TestClient(app.app).get("/api/strategy_notes").json() == {}


def test_strategy_notes_serves_parsed_object(monkeypatch, tmp_path):
    notes = {
        "fundingfade": {
            "thesis": "harvest negative funding with macro gate",
            "exit": "band reclaim",
        },
    }
    path = tmp_path / "strategy_notes.json"
    path.write_text(json.dumps(notes))
    monkeypatch.setattr(app, "_STRATEGY_NOTES_PATH", path)
    assert TestClient(app.app).get("/api/strategy_notes").json() == notes
