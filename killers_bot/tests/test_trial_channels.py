"""Fan-out de canais em trial, capture-only (bd Master-Trader-krl).

O que precisa ser provado:

- um canal capture_only grava o raw e NADA mais: sem classificador, sem
  simulador de papel, sem POST ao receiver
- TRIAL_CHANNELS gera uma config por entrada valida, DB proprio ao lado do
  KILLERS_DB e receiver sempre vazio; entrada invalida e pulada, nao derruba
- TRIAL_CHANNELS ausente desliga o fan-out por completo
- encaminhar e opt-in: so TRIAL_<NAME>_RECEIVER_URL liga o receiver do canal;
  o altsignals usa o parser deterministico e nunca chama o LLM
"""
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from killers_bot import altsignals_rules, observer  # noqa: E402

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

    async def fake_post(*a, **k):
        raise AssertionError("no receiver_url set — nothing to post")

    monkeypatch.setattr(observer, "build_reply_chain", fake_chain)
    monkeypatch.setattr(observer.classifier, "classify", fake_claude)
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


def test_trial_sem_receiver_url_continua_capture_only(base_env, monkeypatch):
    monkeypatch.setenv("TRIAL_CHANNELS", "altsignals:-7001,wolfx:-7002")
    monkeypatch.setenv("TRIAL_ALTSIGNALS_RECEIVER_TOKEN", "tok")
    for name, cfg in observer._trial_configs():
        assert cfg.capture_only is True, name
        assert cfg.receiver_url == "" and cfg.receiver_token == "", name
        assert cfg.fast_path_fn is None and cfg.shadow_llm is True, name


def test_trial_com_receiver_url_encaminha_so_aquele_canal(base_env, monkeypatch):
    monkeypatch.setenv("TRIAL_CHANNELS", "wolfx:-7002,pumps:-7003")
    monkeypatch.setenv("TRIAL_WOLFX_RECEIVER_URL", "http://wolfx-receiver:8089/event")
    monkeypatch.setenv("TRIAL_WOLFX_RECEIVER_TOKEN", "wolfx-token")
    wolfx, pumps = (cfg for _, cfg in observer._trial_configs())
    assert wolfx.capture_only is False
    assert wolfx.receiver_url == "http://wolfx-receiver:8089/event"
    assert wolfx.receiver_token == "wolfx-token"
    assert wolfx.feed_label == "wolfx"
    assert wolfx.fast_path_fn is None
    assert wolfx.shadow_rules is False and wolfx.rules_primary is False
    assert pumps.capture_only is True and pumps.receiver_url == ""


def test_altsignals_com_receiver_url_usa_o_parser_sem_llm(base_env, monkeypatch):
    monkeypatch.setenv("TRIAL_CHANNELS", "altsignals:-7001")
    monkeypatch.setenv("TRIAL_ALTSIGNALS_RECEIVER_URL", "http://altsignals-receiver:8089/event")
    monkeypatch.setenv("TRIAL_ALTSIGNALS_RECEIVER_TOKEN", "alt-token")
    (name, cfg), = observer._trial_configs()
    assert name == "altsignals"
    assert cfg.capture_only is False
    assert cfg.receiver_url == "http://altsignals-receiver:8089/event"
    assert cfg.receiver_token == "alt-token"
    assert cfg.feed_label == "altsignals"
    assert cfg.fast_path_fn is altsignals_rules.classify_altsignals
    assert cfg.shadow_llm is False
    assert cfg.shadow_rules is False and cfg.rules_primary is False
    assert cfg.use_fast_path is False


OPEN_ALT = ("Coin: ETHUSDT Long\nLeverage: Isolated x10\nEntry: 2500.5\n"
            "Target 1: 2550\nTarget 2: 2600\nSL: 2450")
STOP_ALT = "#ETH/USDT Stop Target Hit \u26d4\nLoss: 38.9% \U0001f4c9"


def test_altsignals_fim_a_fim_nunca_chama_o_llm(base_env, monkeypatch):
    monkeypatch.setenv("TRIAL_CHANNELS", "altsignals:-7001")
    monkeypatch.setenv("TRIAL_ALTSIGNALS_RECEIVER_URL", "http://altsignals-receiver:8089/event")
    monkeypatch.setenv("TRIAL_ALTSIGNALS_RECEIVER_TOKEN", "alt-token")
    (_, cfg), = observer._trial_configs()
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    enviados = []

    async def fail_claude(*a, **k):
        raise AssertionError("altsignals must never reach the LLM classifier")

    async def fake_post(url, msg, classification, token=""):
        enviados.append((url, msg, classification, token))

    monkeypatch.setattr(observer.classifier, "classify", fail_claude)
    monkeypatch.setattr(observer, "_post_to_receiver", fake_post)

    t0 = datetime(2026, 10, 2, 8, 17, 15, tzinfo=timezone.utc)
    for mid, text in ((1, OPEN_ALT), (2, OPEN_ALT), (3, STOP_ALT), (4, "Oh lord make it rain")):
        _run_with_date(conn, cfg, mid, text, t0)

    assert [e[2]["kind"] for e in enviados] == ["open", "chat", "close_full", "chat"]
    assert all(e[0] == "http://altsignals-receiver:8089/event" and e[3] == "alt-token"
               for e in enviados)
    assert enviados[1][2]["notes"] == "duplicate_of=1"
    assert enviados[2][2]["symbol"] == "ETH" and enviados[2][2]["pct"] == -38.9

    aberto = enviados[0][1]["text"]
    assert aberto == OPEN_ALT + "\nTARGETS: 2550.0 - 2600.0"
    assert enviados[2][1]["text"] == STOP_ALT

    guardado = conn.execute("SELECT text FROM raw_messages WHERE msg_id = 1").fetchone()[0]
    assert guardado == OPEN_ALT, "the stored raw message keeps no TARGETS line"
    kinds = dict(conn.execute("SELECT msg_id, kind FROM classifications"))
    assert kinds == {1: "open", 2: "chat", 3: "close_full", 4: "chat"}
    assert conn.execute("SELECT COUNT(*) FROM confidence_gate").fetchone()[0] == 0
    conn.close()


def _run_with_date(conn, cfg, mid, text, date):
    async def go():
        await observer.process_message(
            None, None, conn, cfg, {"id": mid, "text": text, "date": date}, "live")
        for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            await t
    asyncio.run(go())


def _fast_path_cfg(shadow_llm):
    return SimpleNamespace(
        capture_only=False, use_fast_path=False, rules_primary=False,
        shadow_rules=False, receiver_url="", receiver_token="",
        confidence_gate=None, claude_binary="claude", claude_model=None,
        claude_timeout=12.0, classifier_template=None,
        fast_path_fn=lambda text, msg_id, conn, date: {
            "id": msg_id, "kind": "chat", "signal_id": None, "symbol": None,
            "direction": None, "confidence": 1.0, "notes": "x", "pct": None},
        **({} if shadow_llm is None else {"shadow_llm": shadow_llm}))


@pytest.mark.parametrize("shadow_llm,esperado", [(False, 0), (True, 1), (None, 1)])
def test_fast_path_fn_e_final_e_shadow_llm_controla_o_shadow(
        base_env, monkeypatch, shadow_llm, esperado):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    shadows = []

    async def fake_chain(*a, **k):
        return []

    async def fail_claude(*a, **k):
        raise AssertionError("fast_path_fn result is final: no LLM fallthrough")

    async def fake_shadow(msg, chain, fast_path, config, conn=None, source_label=""):
        shadows.append(source_label)

    monkeypatch.setattr(observer, "build_reply_chain", fake_chain)
    monkeypatch.setattr(observer.classifier, "classify", fail_claude)
    monkeypatch.setattr(observer, "_shadow_classify", fake_shadow)

    _run(conn, _fast_path_cfg(shadow_llm), 5, "anything at all")

    assert len(shadows) == esperado
    assert conn.execute("SELECT kind FROM classifications").fetchone()[0] == "chat"
    conn.close()
