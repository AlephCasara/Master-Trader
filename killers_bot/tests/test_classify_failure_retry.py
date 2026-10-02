"""Falha de classificacao visivel e retentavel (bd Master-Trader-fg0).

O que precisa ser provado:

- uma classificacao que falha grava linha em `classify_failures` com a
  `error_class` certa e fica `pending` (o retry foi agendado)
- retry bem-sucedido marca `resolved` e segue o caminho normal: a linha chega
  em `classifications`
- esgotadas as tentativas, a linha fica `dropped`, sem classificacao
- o mapa texto de erro → error_class (dns / connect / http / resto)
"""
import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from killers_bot import classifier, observer  # noqa: E402

SCHEMA = (ROOT / "killers_bot" / "schema.sql").read_text()

DNS_ERROR = {"class": "dns_fail", "detail": "getaddrinfo failed"}

CHAT = {"kind": "chat", "symbol": None, "direction": None, "signal_id": None,
        "confidence": 0.9, "notes": "", "pct": None}


class _Cfg:
    use_fast_path = False
    shadow_rules = False
    rules_primary = False
    receiver_url = ""
    receiver_token = ""
    confidence_gate = None
    claude_binary = "claude"
    claude_model = None
    claude_timeout = 1
    classifier_template = None


@pytest.fixture()
def env(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)

    async def fake_chain(*a, **k):
        return []

    monkeypatch.setattr(observer, "build_reply_chain", fake_chain)
    yield conn
    conn.close()


def _run(conn, mid, text):
    """process_message + drenagem do loop de retries (cada retry agenda o
    proximo, entao drena ate nao sobrar tarefa)."""
    async def go():
        await observer.process_message(None, None, conn, _Cfg(),
                                       {"id": mid, "text": text}, "live")
        while True:
            tasks = [t for t in asyncio.all_tasks()
                     if t is not asyncio.current_task()]
            if not tasks:
                return
            await asyncio.gather(*tasks)
    asyncio.run(go())


def test_falha_grava_linha_com_error_class(env, monkeypatch):
    conn = env
    # delay longo: o retry fica dormindo, o teste olha so a primeira gravacao
    monkeypatch.setattr(observer, "RETRY_DELAYS", (3600,))

    async def fail_detailed(msg, chain, **k):
        return None, dict(DNS_ERROR)

    monkeypatch.setattr(observer.classifier, "classify_detailed", fail_detailed)

    async def go():
        await observer.process_message(None, None, conn, _Cfg(),
                                       {"id": 50, "text": "CLOSE"}, "live")
    asyncio.run(go())

    row = conn.execute(
        "SELECT error_class, detail, state, attempts FROM classify_failures "
        "WHERE msg_id = 50").fetchone()
    assert row == ("dns_fail", "getaddrinfo failed", "pending", 1)


def test_retry_com_sucesso_marca_resolvido(env, monkeypatch):
    conn = env
    monkeypatch.setattr(observer, "RETRY_DELAYS", (0, 0, 0))
    calls = {"n": 0}

    async def flaky(msg, chain, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, dict(DNS_ERROR)
        return dict(CHAT, id=msg["id"]), None

    monkeypatch.setattr(observer.classifier, "classify_detailed", flaky)
    _run(conn, 51, "CLOSE")

    state, resolved_at, attempts = conn.execute(
        "SELECT state, resolved_at, attempts FROM classify_failures "
        "WHERE msg_id = 51").fetchone()
    assert state == "resolved" and resolved_at is not None
    assert attempts == 1                      # resolveu no primeiro retry
    assert conn.execute(
        "SELECT COUNT(*) FROM classifications WHERE msg_id = 51"
    ).fetchone()[0] == 1


def test_esgota_tentativas_e_marca_dropped(env, monkeypatch):
    conn = env
    monkeypatch.setattr(observer, "RETRY_DELAYS", (0, 0, 0))

    async def always_fail(msg, chain, **k):
        return None, {"class": "connect_fail", "detail": "Connection refused"}

    monkeypatch.setattr(observer.classifier, "classify_detailed", always_fail)
    _run(conn, 52, "CLOSE")

    row = conn.execute(
        "SELECT state, attempts, resolved_at FROM classify_failures "
        "WHERE msg_id = 52").fetchone()
    assert row == ("dropped", 4, None)        # inicial + 3 retries
    assert conn.execute(
        "SELECT COUNT(*) FROM classifications").fetchone()[0] == 0


@pytest.mark.parametrize("texto, esperado", [
    ("mt-classify: HTTP 429: b'rate limited'", "http_429"),
    ("mt-classify: HTTP 502: b'bad gateway'", "http_error"),
    ("mt-classify: [Errno -3] Temporary failure in name resolution", "dns_fail"),
    ("mt-classify: <urlopen error [Errno -2] Name or service not known>", "dns_fail"),
    ("mt-classify: [Errno 111] Connection refused", "connect_fail"),
    ("mt-classify: unexpected payload {'choices': []}", "unknown"),
])
def test_mapeamento_error_class(texto, esperado):
    assert classifier.error_class_from_text(texto) == esperado
