"""The receiver's outbound HTTP sessions must honor HTTPS_PROXY/NO_PROXY.

On a host geoblocked by Binance (HTTP 451) the futures mark price is only
reachable through an HTTP proxy given by the environment. aiohttp ignores the
proxy variables unless the session is built with `trust_env=True`, and the
mark-price guard fails closed, so one session without it blocks every open.

Without proxy variables set, `trust_env=True` changes nothing.
"""
import ast
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SERVICE_DIR = Path(__file__).resolve().parent.parent


def _client_session_calls():
    for sub in ("app", "warden"):
        for path in sorted((SERVICE_DIR / sub).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "ClientSession"):
                    yield path.name, node


def test_toda_aiohttp_client_session_do_codigo_usa_trust_env():
    calls = list(_client_session_calls())
    assert calls, "expected the receiver to create aiohttp sessions"
    missing = []
    for name, node in calls:
        kw = {k.arg: k.value for k in node.keywords}
        trust = kw.get("trust_env")
        if not (isinstance(trust, ast.Constant) and trust.value is True):
            missing.append(f"{name}:{node.lineno}")
    assert not missing, f"ClientSession without trust_env=True at {missing}"


def test_get_binance_mark_price_avulso_cria_sessao_com_trust_env(monkeypatch):
    import app.main as m

    seen = {}

    class Resp:
        status = 200

        async def json(self):
            return {"markPrice": "2500.5"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def get(self, url, **kwargs):
            return Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(m.aiohttp, "ClientSession", FakeSession)
    assert asyncio.run(m.get_binance_mark_price("ETH")) == 2500.5
    assert seen.get("trust_env") is True


def test_sessoes_persistentes_do_lifespan_usam_trust_env():
    tf = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    os.environ["KILLERS_DB"] = tf.name
    os.environ["KILLERS_NOTIFY_URL"] = ""
    from importlib import reload

    import app.main as m
    reload(m)
    from fastapi.testclient import TestClient

    with TestClient(m.app):
        assert m.app.state.ft_session.trust_env is True
        assert m.app.state.public_session.trust_env is True
