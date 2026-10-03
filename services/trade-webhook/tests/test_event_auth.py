"""POST /freqtrade/event authentication and record integrity (#97).

The route turns caller text into an ops Telegram line, a lake record and,
for losing exit_fill events, a pair-drift alert. It was open to anything that
could reach the container. It now checks the #59 shared secret, from the
X-Notify-Token header or, for Freqtrade (which cannot send headers), from a
`webhook_token` body field:

  TRADE_WEBHOOK_EVENT_AUTH=monitor (default): accept, warn when unauthenticated
  TRADE_WEBHOOK_EVENT_AUTH=enforce: 401, nothing stored, nothing sent

The token is stripped from every payload before it is stored or forwarded,
and the server's `ts` / `received_from` can no longer be overwritten.
"""
import importlib
import json
import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

TOKEN = "notify-token-for-tests-0123456789abcdef"

# What a Freqtrade 2026.7 bot posts: its webhookexitfill template formatted
# with the trade (every value a string), plus the env-injected token.
FT_EXIT_FILL = {
    "type": "exit_fill",
    "bot_name": "KeltnerBounceV1",
    "pair": "ARB/USDT",
    "profit_ratio": "-0.0123",
    "profit_amount": "-0.1845",
    "stake_currency": "USDT",
    "open_rate": "0.51",
    "close_rate": "0.5037",
    "exit_reason": "stop_loss",
    "direction": "Long",
}


def _load(monkeypatch, tmp_path, token, mode=None):
    """Import app.py fresh so its import-time env reads see token and mode."""
    monkeypatch.setenv("TRADES_DIR", str(tmp_path))
    monkeypatch.delenv("OPS_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OPS_BOT_CHAT_ID", raising=False)
    if token is None:
        monkeypatch.delenv("TRADE_WEBHOOK_NOTIFY_TOKEN", raising=False)
    else:
        monkeypatch.setenv("TRADE_WEBHOOK_NOTIFY_TOKEN", token)
    if mode is None:
        monkeypatch.delenv("TRADE_WEBHOOK_EVENT_AUTH", raising=False)
    else:
        monkeypatch.setenv("TRADE_WEBHOOK_EVENT_AUTH", mode)
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    sent = []

    async def fake_send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(module, "telegram_send", fake_send)
    return module, TestClient(module.app), sent


@pytest.fixture(autouse=True)
def _drop_module():
    yield
    sys.modules.pop("app", None)


def _lake(tmp_path):
    return "".join(p.read_text() for p in sorted(tmp_path.glob("*.jsonl")))


@pytest.mark.parametrize("headers, body_extra", [
    ({}, {}),
    ({"X-Notify-Token": "wrong"}, {}),
    ({"X-Notify-Token": TOKEN[:-1]}, {}),
    ({"Authorization": f"Bearer {TOKEN}"}, {}),
    ({}, {"webhook_token": ""}),
    ({}, {"webhook_token": "wrong"}),
    ({}, {"webhook_token": TOKEN + "x"}),
    # Freqtrade types numeric-looking env values: never coerced to match.
    ({}, {"webhook_token": 123456}),
    ({}, {"webhook_token": [TOKEN]}),
])
def test_enforce_rejects_and_stores_nothing(monkeypatch, tmp_path, headers, body_extra):
    _, client, sent = _load(monkeypatch, tmp_path, TOKEN, "enforce")
    forged = {"type": "status", "bot_name": "fleet-circuit-breaker",
              "status": "CIRCUIT BREAKER: forged", **body_extra}
    r = client.post("/freqtrade/event", json=forged, headers=headers)
    assert r.status_code == 401
    assert sent == []
    assert list(tmp_path.iterdir()) == []


def test_enforce_blocks_forged_losses_from_raising_a_pair_drift_alert(monkeypatch, tmp_path):
    _, client, sent = _load(monkeypatch, tmp_path, TOKEN, "enforce")
    for _ in range(4):
        assert client.post("/freqtrade/event", json=FT_EXIT_FILL).status_code == 401
    assert sent == []
    assert list(tmp_path.iterdir()) == []


def test_enforce_accepts_header_token(monkeypatch, tmp_path):
    _, client, sent = _load(monkeypatch, tmp_path, TOKEN, "enforce")
    r = client.post("/freqtrade/event",
                    json={"type": "status", "bot_name": "health-report", "status": "ok"},
                    headers={"X-Notify-Token": TOKEN})
    assert r.status_code == 200
    assert sent == ["[health-report] STATUS ok"]


def test_enforce_accepts_freqtrade_body_token_and_never_keeps_it(monkeypatch, tmp_path, caplog):
    _, client, sent = _load(monkeypatch, tmp_path, TOKEN, "enforce")
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            r = client.post("/freqtrade/event", json={**FT_EXIT_FILL, "webhook_token": TOKEN})
            assert r.status_code == 200
        # entry_cancel falls through summarize()'s generic branch, which prints
        # payload keys; the token must not be among them.
        r = client.post("/freqtrade/event", json={
            "type": "entry_cancel", "bot_name": "KeltnerBounceV1", "pair": "ARB/USDT",
            "reason": "timeout", "webhook_token": TOKEN})
        assert r.status_code == 200
    records = [json.loads(line) for line in
               (tmp_path / "KeltnerBounceV1.jsonl").read_text().splitlines()]
    assert len(records) == 4
    assert all("webhook_token" not in rec for rec in records)
    assert TOKEN not in _lake(tmp_path)
    assert all(TOKEN not in text for text in sent)
    assert TOKEN not in caplog.text
    # The real events still drive the pair-drift detector.
    assert any(text.startswith("⚠ PAIR DRIFT [KeltnerBounceV1] ARB/USDT") for text in sent)


def test_monitor_accepts_but_warns_when_unauthenticated(monkeypatch, tmp_path, caplog):
    _, client, sent = _load(monkeypatch, tmp_path, TOKEN)
    with caplog.at_level(logging.WARNING, logger="trade-webhook"):
        r = client.post("/freqtrade/event",
                        json={"type": "status", "bot_name": "OITrendPullbackV1",
                              "status": "up", "webhook_token": "stale-value"})
    assert r.status_code == 200
    assert sent == ["[OITrendPullbackV1] STATUS up"]
    assert "unauthenticated POST /freqtrade/event bot=OITrendPullbackV1" in caplog.text
    assert "invalid token" in caplog.text
    assert "stale-value" not in caplog.text
    assert "stale-value" not in _lake(tmp_path)


def test_monitor_is_quiet_for_authenticated_events(monkeypatch, tmp_path, caplog):
    _, client, _ = _load(monkeypatch, tmp_path, TOKEN, "monitor")
    with caplog.at_level(logging.WARNING, logger="trade-webhook"):
        client.post("/freqtrade/event", json={**FT_EXIT_FILL, "webhook_token": TOKEN})
        client.post("/freqtrade/event", json={"type": "status", "bot_name": "x", "status": "y"},
                    headers={"X-Notify-Token": TOKEN})
    assert "unauthenticated POST" not in caplog.text


def test_no_token_keeps_the_route_open_and_strips_the_field(monkeypatch, tmp_path, caplog):
    """Rollout step 1: deployed before any token exists, so a bot that already
    carries one (or an empty one) never has it written to the lake."""
    with caplog.at_level(logging.WARNING, logger="trade-webhook"):
        _, client, sent = _load(monkeypatch, tmp_path, None)
    assert "accepts unauthenticated events" in caplog.text
    r = client.post("/freqtrade/event", json={**FT_EXIT_FILL, "webhook_token": TOKEN})
    assert r.status_code == 200
    assert TOKEN not in _lake(tmp_path)
    assert len(sent) == 1


def test_enforce_without_a_token_refuses_to_start(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="needs TRADE_WEBHOOK_NOTIFY_TOKEN"):
        _load(monkeypatch, tmp_path, None, "enforce")


@pytest.mark.parametrize("mode", ["enforced", "on", "true"])
def test_unknown_mode_refuses_to_start(monkeypatch, tmp_path, mode):
    with pytest.raises(RuntimeError, match="TRADE_WEBHOOK_EVENT_AUTH must be one of"):
        _load(monkeypatch, tmp_path, TOKEN, mode)


def test_server_fields_cannot_be_overwritten_by_the_payload(monkeypatch, tmp_path):
    _, client, _ = _load(monkeypatch, tmp_path, TOKEN, "enforce")
    r = client.post("/freqtrade/event",
                    json={"type": "status", "bot_name": "b", "status": "x",
                          "ts": "2020-01-01T00:00:00Z", "received_from": "10.9.9.9"},
                    headers={"X-Notify-Token": TOKEN})
    assert r.status_code == 200
    rec = json.loads((tmp_path / "b.jsonl").read_text())
    assert rec["ts"] != "2020-01-01T00:00:00Z"
    assert rec["received_from"] == "testclient"


def test_notify_route_unchanged_by_event_auth(monkeypatch, tmp_path):
    _, client, sent = _load(monkeypatch, tmp_path, TOKEN, "enforce")
    assert client.post("/test/notify", json={"text": "x"}).status_code == 401
    r = client.post("/test/notify", json={"text": "hi"}, headers={"X-Notify-Token": TOKEN})
    assert r.status_code == 200
    assert sent == ["hi"]
    assert client.get("/healthz").status_code == 200
