"""Fan-out de canais em trial, capture-only (bd Master-Trader-krl).

O que precisa ser provado:

- um canal capture_only grava o raw e NADA mais: sem classificador, sem
  simulador de papel, sem POST ao receiver
- TRIAL_CHANNELS gera uma config por entrada valida, DB proprio ao lado do
  KILLERS_DB e receiver sempre vazio; entrada invalida e pulada, nao derruba
- TRIAL_CHANNELS ausente desliga o fan-out por completo
"""
import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from killers_bot import observer  # noqa: E402

SCHEMA = (ROOT / "killers_bot" / "schema.sql").read_text()


@pytest.fixture()
def base_env(monkeypatch):
    for var, val in (("KILLERS_TG_API_ID", "1"), ("KILLERS_TG_API_HASH", "x"),
                     ("KILLERS_TG_SESSION", "/tmp/x.session")):
        monkeypatch.setenv(var, val)
    monkeypatch.delenv("TRIAL_CHANNELS", raising=False)
    monkeypatch.delenv("KILLERS_DB", raising=False)


def _run(conn, cfg, mid, text):
    async def go():
        await observer.process_message(None, None, conn, cfg, {"id": mid, "text": text}, "live")
        for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            await t
    asyncio.run(go())


def test_capture_only_para_depois_do_raw(base_env, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)

    async def fail_claude(*a, **k):
        raise AssertionError("classifier must not run for capture-only channels")

    async def fail_post(*a, **k):
        raise AssertionError("receiver POST must not run for capture-only channels")

    monkeypatch.setattr(observer.classifier, "classify", fail_claude)
    monkeypatch.setattr(observer, "_post_to_receiver", fail_post)

    cfg = SimpleNamespace(capture_only=True, use_fast_path=False,
                          rules_primary=False, shadow_rules=False,
                          receiver_url="", receiver_token="")
    _run(conn, cfg, 7, "$BTC LONG\nENTRY 42000\nTARGETS 43500\nSTOP 41000")

    raw = conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]
    cls = conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
    paper = conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
    assert (raw, cls, paper) == (1, 0, 0)
    conn.close()


def test_canal_normal_continua_alem_do_raw(base_env, monkeypatch):
    """Sanity do lado oposto: sem capture_only o classificador roda."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)

    async def fake_chain(*a, **k):
        return []

    async def fake_claude(msg, chain, **k):
        return {"id": msg["id"], "kind": "chat", "symbol": None, "direction": None,
                "signal_id": None, "confidence": 0.9, "notes": "", "pct": None}

    async def fake_detailed(msg, chain, **k):
        return await fake_claude(msg, chain, **k), None

    async def fake_post(*a, **k):
        raise AssertionError("no receiver_url set — nothing to post")

    monkeypatch.setattr(observer, "build_reply_chain", fake_chain)
    monkeypatch.setattr(observer.classifier, "classify_detailed", fake_detailed)
    monkeypatch.setattr(observer, "_post_to_receiver", fake_post)

    cfg = SimpleNamespace(capture_only=False, use_fast_path=False,
                          rules_primary=False, shadow_rules=False,
                          receiver_url="", receiver_token="",
                          confidence_gate=None, claude_binary="claude",
                          claude_model=None, claude_timeout=12.0,
                          classifier_template=None)
    _run(conn, cfg, 8, "random chat message")

    cls = conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
    assert cls == 1
    conn.close()


def test_trial_channels_gera_uma_config_por_entrada_valida(base_env, monkeypatch):
    monkeypatch.setenv("KILLERS_DB", "/data/killers/state.sqlite")
    monkeypatch.setenv(
        "TRIAL_CHANNELS",
        "wolfx:-1001394941879, altsignals:-1001954348590, bad-entry, :123, x:abc, ,")
    trials = observer._trial_configs()
    assert [n for n, _ in trials] == ["wolfx", "altsignals"]
    wolfx = trials[0][1]
    assert wolfx.channel_id_override == "-1001394941879"
    assert wolfx.channel_username is None
    assert wolfx.db_path == "/data/killers/trial-wolfx-state.sqlite"
    assert wolfx.capture_only is True
    assert wolfx.receiver_url == "" and wolfx.receiver_token == ""
    assert wolfx.use_fast_path is False and wolfx.rules_primary is False
    assert wolfx.shadow_rules is False
    altsignals = trials[1][1]
    assert altsignals.db_path == "/data/killers/trial-altsignals-state.sqlite"


def test_trial_channels_db_default_segue_o_killers_db(base_env, monkeypatch):
    monkeypatch.delenv("KILLERS_DB", raising=False)
    monkeypatch.setenv("TRIAL_CHANNELS", "pumps:-1001622651246")
    (name, cfg), = observer._trial_configs()
    assert name == "pumps"
    assert cfg.db_path == "/var/lib/killers/trial-pumps-state.sqlite"


def test_trial_channels_ausente_desliga(base_env):
    assert observer._trial_configs() == []
