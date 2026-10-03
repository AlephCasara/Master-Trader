"""Every ft_userdata report sender reaches trade-webhook's real route (#101).

Seven scripts posted to http://localhost:8088/webhooks/freqtrade, the Mac
claude-assistant route from before the VPS move. trade-webhook serves
/freqtrade/event and parses the body as JSON, so they got 404 (and the six
form-encoded senders would have got 400 even on the right path). No test
called any of them.

Here each sender's own send function runs unmodified; only the transport is
swapped so its request lands on the real trade-webhook app through FastAPI's
TestClient (Telegram stubbed, lake written to tmp_path).
"""

import importlib
import importlib.util
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
FT_DIR = ROOT / "ft_userdata"
TRADE_WEBHOOK = ROOT / "services" / "trade-webhook" / "app.py"
TOKEN = "notify-token-for-tests-0123456789abcdef"


@pytest.fixture
def webhook(monkeypatch, tmp_path):
    """The real trade-webhook app on tmp storage, reached through requests.post."""
    lake = tmp_path / "lake"
    monkeypatch.setenv("TRADES_DIR", str(lake))
    for var in ("OPS_BOT_TOKEN", "OPS_BOT_CHAT_ID", "TRADE_WEBHOOK_NOTIFY_TOKEN",
                "WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)
    spec = importlib.util.spec_from_file_location("trade_webhook_for_sender_tests", TRADE_WEBHOOK)
    app_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_module)

    telegram = []

    async def fake_send(text):
        telegram.append(text)
        return True

    monkeypatch.setattr(app_module, "telegram_send", fake_send)
    client = TestClient(app_module.app)
    calls = []

    def post(url, data=None, json=None, headers=None, timeout=None, **_):
        parts = urlsplit(url)
        calls.append({"url": url, "headers": dict(headers or {}), "json": json, "data": data})
        assert parts.netloc == "localhost:8088", url
        if json is not None:
            return client.post(parts.path, json=json, headers=headers)
        return client.post(parts.path, data=data, headers=headers)

    # Patched on the module object, so `requests.post`, `import requests as
    # req; req.post` and the shared helper all go through it.
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.syspath_prepend(str(FT_DIR))
    return {"lake": lake, "telegram": telegram, "calls": calls}


def _load_script(name):
    spec = importlib.util.spec_from_file_location(f"{name}_for_sender_tests", FT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine_reporting():
    return importlib.import_module("engine.reporting")


# (loader, how to make it send, bot_name it must report as)
SENDERS = {
    "engine": (_engine_reporting, lambda m: m.send_telegram("engine report"), "backtest-engine"),
    "tournament": (lambda: _load_script("tournament_manager"),
                   lambda m: m.send_telegram_report([], {}, 0.0, 0.0, dry_run=True),
                   "tournament-manager"),
    "rotator": (lambda: _load_script("bot_rotator"),
                lambda m: m.send_report([], [], dry_run=True), "bot-rotator"),
    "ai_health": (lambda: _load_script("ai_health_report"),
                  lambda m: m.send_telegram("ai health report"), "ai-health-report"),
    "walk_forward": (lambda: _load_script("walk_forward"),
                     lambda m: m.send_telegram("walk-forward report"), "walk-forward"),
    "backtest_gate": (lambda: _load_script("backtest_gate"),
                      lambda m: m.send_telegram("backtest gate report"), "backtest-gate"),
    "hyperopt": (lambda: _load_script("hyperopt_optimizer"),
                 lambda m: m.send_telegram("hyperopt report"), "hyperopt-optimizer"),
    "health_report": (lambda: _load_script("strategy_health_report"),
                      lambda m: m.send_telegram("health report"), "health-report"),
}


@pytest.mark.parametrize("sender", sorted(SENDERS))
def test_sender_reaches_trade_webhook(webhook, sender):
    load, send, bot_name = SENDERS[sender]
    result = send(load())

    assert len(webhook["calls"]) == 1
    call = webhook["calls"][0]
    assert call["url"] == "http://localhost:8088/freqtrade/event"
    assert call["json"] is not None and call["data"] is None, "must send JSON, not a form"
    # bot_rotator.send_report returns nothing; every other sender reports success.
    if result is not None:
        assert result is True
    assert len(webhook["telegram"]) == 1
    assert webhook["telegram"][0].startswith(f"[{bot_name}] STATUS ")
    lines = (webhook["lake"] / f"{bot_name}.jsonl").read_text().splitlines()
    event = json.loads(lines[-1])
    assert event["type"] == "status" and event["bot_name"] == bot_name
    assert not (webhook["lake"] / "unknown.jsonl").exists()


def test_webhook_url_env_overrides_default(webhook, monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "http://localhost:8088/freqtrade/event?via=env")
    notify = _load_script("webhook_notify")
    assert notify.send_status("x", bot_name="probe") is True
    assert webhook["calls"][0]["url"].endswith("?via=env")


def test_notify_token_sent_only_when_configured(webhook, monkeypatch):
    notify = _load_script("webhook_notify")
    notify.send_status("x", bot_name="probe")
    assert "X-Notify-Token" not in webhook["calls"][0]["headers"]

    monkeypatch.setenv("TRADE_WEBHOOK_NOTIFY_TOKEN", TOKEN)
    notify.send_status("y", bot_name="probe")
    assert webhook["calls"][1]["headers"]["X-Notify-Token"] == TOKEN


@pytest.mark.parametrize("sender", ["walk_forward", "backtest_gate", "hyperopt"])
def test_rejected_report_is_logged(webhook, monkeypatch, caplog, sender):
    """These three returned False on a non-2xx without a word in the log."""
    monkeypatch.setenv("WEBHOOK_URL", "http://localhost:8088/no-such-route")
    load, send, bot_name = SENDERS[sender]
    module = load()
    with caplog.at_level(logging.WARNING):
        assert send(module) is False
    assert f"{bot_name} report not sent" in caplog.text
    assert "HTTP 404" in caplog.text


def test_no_sender_hardcodes_a_webhook_url():
    """One URL, from the environment: seven hardcoded copies went stale together."""
    literal = re.compile(r"^\s*WEBHOOK_URL\s*=\s*[\"']", re.MULTILINE)
    offenders = []
    for path in sorted(FT_DIR.rglob("*.py")):
        if path.name == "webhook_notify.py":
            continue
        text = path.read_text(errors="replace")
        if "/webhooks/freqtrade" in text or literal.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
