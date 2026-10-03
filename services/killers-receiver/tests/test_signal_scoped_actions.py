"""#96: a message acts only on its own signal's position, and only once.

Reproduces the issue's table against `_process_event` with Freqtrade
mocked. Before the fix, `find_active_position` fell back to any single
active position on the symbol even when the message named a different
signal, the #64 one-action check was per-position, and `move_sl` wrote no
`events` row.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main as receiver_main  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _restore_app_state():
    saved = getattr(receiver_main.app, "state", None)
    try:
        yield
    finally:
        if saved is not None:
            receiver_main.app.state = saved


class _FakeState:
    def __init__(self, c, cf):
        self.conn = c
        self.cfg = cf
        self.ft_session = None
        self.public_session = None
        self.notify_tasks = set()
        self.phase2_lock = None


def _setup():
    tf = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    os.environ["KILLERS_DB"] = tf.name
    os.environ["KILLERS_ACTIVE_TP_LIMITS"] = "false"
    cfg = receiver_main.Config()
    conn = receiver_main.init_db(cfg.db_path)
    receiver_main.app.state = _FakeState(conn, cfg)
    return cfg, conn


def _add_position(conn, signal_id, symbol, open_msg, ft_trade_id,
                  state="open", sl_abs=None):
    conn.execute(
        "INSERT INTO positions (signal_id, symbol, pair, direction, state, "
        " open_msg_id, open_date, stake_usd, leverage, ft_trade_id, pct_open, sl_abs) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (signal_id, symbol, f"{symbol}/USDC:USDC", "long", state, open_msg,
         "2026-07-01T20:00:00+00:00", 20.0, 3.0, ft_trade_id,
         100 if state == "open" else 0, sl_abs),
    )
    return conn.execute("SELECT pos_id FROM positions WHERE open_msg_id=?",
                        (open_msg,)).fetchone()[0]


def _payload(kind, msg_id, signal_id, symbol, **extra):
    cls = {"kind": kind, "signal_id": signal_id, "symbol": symbol,
           "direction": "long"}
    cls.update(extra)
    return receiver_main.EventPayload(
        msg={"id": msg_id, "date": "2026-07-01T21:00:00+00:00", "text": "x"},
        classification=cls,
    )


class _FT:
    def __init__(self):
        self.exits = []

    async def force_exit(self, _cfg, trade_id, pct=None, session=None, **_kw):
        self.exits.append(trade_id)
        return {"status": 200, "body": '{"result":"ok"}'}


async def _mark(_symbol, session=None):
    return 1.20


def _process(ft, payload):
    with patch.object(receiver_main, "ft_force_exit", side_effect=ft.force_exit), \
         patch.object(receiver_main, "get_execution_mark_price", side_effect=_mark):
        return _run(receiver_main._process_event(payload))


def _state(conn, pos_id):
    return conn.execute("SELECT state FROM positions WHERE pos_id=?",
                        (pos_id,)).fetchone()["state"]


# ── find_active_position ────────────────────────────────────────────────


def test_named_signal_never_falls_back_to_another_signals_position():
    _cfg, conn = _setup()
    _add_position(conn, 3001, "KITE", 100, 41, state="closed")
    _add_position(conn, 3002, "KITE", 101, 42)
    pos, reason = receiver_main.find_active_position(conn, 3001, "KITE")
    assert pos is None
    assert reason == "other_signal_only"


def test_named_signal_with_no_position_on_symbol_is_no_match():
    _cfg, conn = _setup()
    pos, reason = receiver_main.find_active_position(conn, 3001, "KITE")
    assert pos is None and reason == "no_match"


def test_named_signal_matches_its_own_position():
    _cfg, conn = _setup()
    _add_position(conn, 3001, "KITE", 100, 41)
    _add_position(conn, 3002, "KITE", 101, 42)
    pos, reason = receiver_main.find_active_position(conn, 3002, "KITE")
    assert reason == "matched_by_signal_id"
    assert pos["ft_trade_id"] == 42


def test_message_without_signal_keeps_the_unique_symbol_fallback():
    _cfg, conn = _setup()
    _add_position(conn, None, "BTC", 100, 41)
    pos, reason = receiver_main.find_active_position(conn, None, "BTC")
    assert reason == "matched_by_symbol_unique"
    assert pos["ft_trade_id"] == 41


def test_message_without_signal_still_refuses_ambiguity():
    _cfg, conn = _setup()
    _add_position(conn, None, "BTC", 100, 41)
    _add_position(conn, None, "BTC", 101, 42)
    pos, reason = receiver_main.find_active_position(conn, None, "BTC")
    assert pos is None and reason == "ambiguous"


# ── the issue's reproduction table ──────────────────────────────────────


def test_first_delivery_of_late_close_does_not_exit_the_newer_signal():
    """Row 1: venue stop closed #3001, #3002 opened KITE, then a late
    `close_full #3001 KITE` arrives. It must not touch #3002."""
    _cfg, conn = _setup()
    _add_position(conn, 3001, "KITE", 100, 41, state="closed")
    newer = _add_position(conn, 3002, "KITE", 101, 42)
    ft = _FT()
    r = _process(ft, _payload("close_full", 500, 3001, "KITE"))
    assert r["action"] == "skipped"
    assert r["reason"] == "no_active_position (other_signal_only)"
    assert ft.exits == []
    assert _state(conn, newer) == "open"


def test_unchanged_redelivery_does_not_exit_a_second_trade():
    """Row 2: `close_full #3001 KITE` delivered twice while #3002 KITE is
    open. The first closes #3001; the second must not close #3002."""
    _cfg, conn = _setup()
    first = _add_position(conn, 3001, "KITE", 100, 41)
    second = _add_position(conn, 3002, "KITE", 101, 42)
    ft = _FT()
    r1 = _process(ft, _payload("close_full", 500, 3001, "KITE"))
    assert r1["action"] == "force_exit" and r1["pos_id"] == first
    r2 = _process(ft, _payload("close_full", 500, 3001, "KITE"))
    assert r2["action"] == "skipped"
    assert ft.exits == [41]
    assert _state(conn, second) == "open"


def test_close_edited_to_another_symbol_does_not_exit_that_symbol():
    """Row 3: `close_full #3001 KITE` edited to ARB with one ARB position
    (another signal's) open."""
    _cfg, conn = _setup()
    _add_position(conn, 3001, "KITE", 100, 41)
    arb = _add_position(conn, 3005, "ARB", 101, 45)
    ft = _FT()
    assert _process(ft, _payload("close_full", 500, 3001, "KITE"))["action"] == "force_exit"
    r2 = _process(ft, _payload("close_full", 500, 3001, "ARB"))
    assert r2["action"] == "skipped"
    assert ft.exits == [41]
    assert _state(conn, arb) == "open"


def test_move_sl_is_the_messages_one_action():
    """Row 4: `move_sl` then edited into `close_full`. The move is recorded
    and the edited close is refused."""
    _cfg, conn = _setup()
    pos_id = _add_position(conn, 3001, "KITE", 100, 41, sl_abs=0.90)
    ft = _FT()
    r1 = _process(ft, _payload("move_sl", 500, 3001, "KITE", sl=1.0))
    assert r1["action"] == "stop_updated"
    kinds = [r[0] for r in conn.execute(
        "SELECT kind FROM events WHERE pos_id=? AND msg_id=?", (pos_id, 500))]
    assert kinds == ["move_sl"]
    assert conn.execute("SELECT sl_abs FROM positions WHERE pos_id=?",
                        (pos_id,)).fetchone()[0] == 1.0

    r2 = _process(ft, _payload("close_full", 500, 3001, "KITE"))
    assert r2["action"] == "deduped"
    assert r2["prior_kind"] == "move_sl"
    assert ft.exits == []
    assert _state(conn, pos_id) == "open"


def test_move_sl_redelivery_is_deduped():
    _cfg, conn = _setup()
    pos_id = _add_position(conn, 3001, "KITE", 100, 41, sl_abs=0.90)
    ft = _FT()
    assert _process(ft, _payload("move_sl", 500, 3001, "KITE", sl=1.0))["action"] == "stop_updated"
    r2 = _process(ft, _payload("move_sl", 500, 3001, "KITE", sl=1.0))
    assert r2 == {"action": "deduped", "pos_id": pos_id, "kind": "move_sl"}


def test_message_that_closed_cannot_move_a_stop():
    _cfg, conn = _setup()
    pos_id = _add_position(conn, 3001, "KITE", 100, 41, sl_abs=0.90)
    ft = _FT()
    assert _process(ft, _payload("close_partial", 500, 3001, "KITE", pct=50))["action"] == "force_exit"
    r2 = _process(ft, _payload("move_sl", 500, 3001, "KITE", sl=1.0))
    assert r2["action"] == "deduped" and r2["prior_kind"] == "close_partial"
    assert conn.execute("SELECT sl_abs FROM positions WHERE pos_id=?",
                        (pos_id,)).fetchone()[0] == 0.90


def test_move_sl_naming_another_signal_does_not_move_the_newer_stop():
    _cfg, conn = _setup()
    _add_position(conn, 3001, "KITE", 100, 41, state="closed", sl_abs=0.90)
    newer = _add_position(conn, 3002, "KITE", 101, 42, sl_abs=0.80)
    ft = _FT()
    r = _process(ft, _payload("move_sl", 500, 3001, "KITE", sl=1.0))
    assert r["action"] == "skipped"
    assert conn.execute("SELECT sl_abs FROM positions WHERE pos_id=?",
                        (newer,)).fetchone()[0] == 0.80


def test_close_partial_edited_to_close_full_control_still_refused():
    """Row 5 (control): #64 behaviour unchanged."""
    _cfg, conn = _setup()
    pos_id = _add_position(conn, 3001, "KITE", 100, 41)
    ft = _FT()
    assert _process(ft, _payload("close_partial", 500, 3001, "KITE", pct=50))["action"] == "force_exit"
    r2 = _process(ft, _payload("close_full", 500, 3001, "KITE"))
    assert r2["action"] == "deduped" and r2["prior_kind"] == "close_partial"
    assert r2["prior_pos_id"] == pos_id
    assert ft.exits == [41]


# ── message-wide one-action check (signal-less messages) ────────────────


def test_signalless_close_redelivered_after_a_new_open_does_not_close_it():
    """The fallback's intended use: a close with no signal id. Once it has
    closed one position, a redelivery must not act on the next position that
    takes the coin."""
    _cfg, conn = _setup()
    first = _add_position(conn, None, "BTC", 100, 41)
    ft = _FT()
    r1 = _process(ft, _payload("close_full", 500, None, "BTC"))
    assert r1["action"] == "force_exit" and r1["pos_id"] == first
    second = _add_position(conn, None, "BTC", 101, 42)
    r2 = _process(ft, _payload("close_full", 500, None, "BTC"))
    assert r2["action"] == "deduped"
    assert r2["prior_pos_id"] == first and r2["pos_id"] == second
    assert ft.exits == [41]
    assert _state(conn, second) == "open"


def test_another_positions_open_message_cannot_close_this_one():
    _cfg, conn = _setup()
    opener = _add_position(conn, None, "BTC", 100, 41, state="closed")
    other = _add_position(conn, None, "BTC", 101, 42)
    ft = _FT()
    r = _process(ft, _payload("close_full", 100, None, "BTC"))
    assert r["action"] == "deduped"
    assert r["prior_kind"] == "open" and r["prior_pos_id"] == opener
    assert ft.exits == []
    assert _state(conn, other) == "open"


def test_cross_position_refusal_alert_names_the_prior_position():
    cfg, _conn = _setup()
    payload = _payload("close_full", 500, None, "BTC")
    result = {"action": "deduped", "pos_id": 8, "kind": "close_full",
              "prior_kind": "close_full", "prior_pos_id": 7,
              "reason": "msg already acted as close_full on pos 7"}
    text = receiver_main._format_event_summary(cfg, payload, result)
    assert text is not None
    assert "pos=7" in text and "pos=8" in text
