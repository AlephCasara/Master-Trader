"""#126: every Killers exit carries an attributable reason.

Freqtrade 2026.7's /forceexit payload is {tradeid, ordertype, amount, price}
— no exit tag — so Freqtrade records TP fills, channel closes, posted-SL
exits and an operator's manual close all as exit_reason=force_exit. The
receiver records each exit it submits (target_orders for the TP ladder,
exit_requests for everything else) and `attribute_exit_orders` maps
Freqtrade's exit orders back to those records. Freqtrade is mocked.
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main as receiver_main  # noqa: E402

SIGNAL_TARGETS = [0.105, 0.11, 0.1175, 0.125]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _restore_app_state(monkeypatch):
    # monkeypatch restores these after each test, so no mode leaks out.
    for key in ("KILLERS_DB", "KILLERS_ACTIVE_TP_LIMITS", "KILLERS_POSTED_SL"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
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


def _setup(active_tp=False, posted_sl=False):
    tf = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    os.environ["KILLERS_DB"] = tf.name
    os.environ["KILLERS_ACTIVE_TP_LIMITS"] = "true" if active_tp else "false"
    os.environ["KILLERS_POSTED_SL"] = "true" if posted_sl else "false"
    cfg = receiver_main.Config()
    conn = receiver_main.init_db(cfg.db_path)
    # Ledger started well before the orders in these tests.
    conn.execute("UPDATE receiver_meta SET value=? WHERE key='exit_ledger_since'",
                 ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),))
    conn.execute(
        "INSERT INTO positions (signal_id, symbol, pair, direction, state, "
        " open_msg_id, open_date, stake_usd, leverage, ft_trade_id, pct_open, sl_abs) "
        "VALUES (2234, 'POL', 'POL/USDC:USDC', 'long', 'open', 100, "
        " '2026-09-23T23:56:05+00:00', 8.0, 2.0, 9, 100, 0.09)")
    pos_id = conn.execute("SELECT pos_id FROM positions").fetchone()[0]
    conn.execute(
        "INSERT INTO events (pos_id, msg_id, event_at, kind, payload, response) "
        "VALUES (?, 100, '2026-09-23T23:56:05+00:00', 'open', ?, '{}')",
        (pos_id, json.dumps({"signal_targets": SIGNAL_TARGETS})))
    receiver_main.app.state = _FakeState(conn, cfg)
    return cfg, conn, pos_id


def _ms(dt=None):
    return int((dt or datetime.now(timezone.utc)).timestamp() * 1000)


def _order(order_id, ts_ms, side="sell", tag="force_exit", order_type="limit",
           status="closed", price=0.1):
    # Hyperliquid market orders are recorded by Freqtrade as `limit`.
    return {"order_id": order_id, "ft_order_side": side, "ft_order_tag": tag,
            "order_type": order_type, "status": status, "filled": 10.0,
            "amount": 10.0, "safe_price": price, "price": price,
            "order_timestamp": ts_ms, "order_date": "x"}


def _trade(orders, is_open=False):
    entry = _order("entry-1", _ms() - 3_600_000, side="buy",
                   tag="signal:2234|sl:0.09")
    return {"trade_id": 9, "pair": "POL/USDC:USDC", "is_short": False,
            "is_open": is_open, "exit_reason": "force_exit",
            "orders": [entry] + orders}


def _attribute(conn, pos_id, trade):
    targets = [dict(r) for r in conn.execute(
        "SELECT * FROM target_orders WHERE pos_id=?", (pos_id,))]
    requests = [dict(r) for r in conn.execute(
        "SELECT * FROM exit_requests WHERE pos_id=?", (pos_id,))]
    since = conn.execute("SELECT value FROM receiver_meta "
                         "WHERE key='exit_ledger_since'").fetchone()[0]
    return receiver_main.attribute_exit_orders(trade, targets, requests,
                                               SIGNAL_TARGETS, since)


def _close_payload(kind, msg_id, **extra):
    cls = {"kind": kind, "signal_id": 2234, "symbol": "POL", "direction": "long"}
    cls.update(extra)
    return receiver_main.EventPayload(
        msg={"id": msg_id, "date": "2026-09-24T14:00:00+00:00", "text": "x"},
        classification=cls)


async def _ok_exit(_cfg, trade_id, pct=None, session=None, **_kw):
    return {"status": 200, "body": '{"result":"Created exit order"}'}


# ── each receiver exit path records its reason ─────────────────────────────


@pytest.mark.parametrize("kind,reason", [
    ("close_full", "channel_close"),
    ("close_partial", "channel_partial_close"),
])
def test_channel_close_is_attributed(kind, reason):
    _cfg, conn, pos_id = _setup()
    with patch.object(receiver_main, "ft_force_exit", side_effect=_ok_exit):
        r = _run(receiver_main._process_event(_close_payload(kind, 500, pct=50)))
    assert r["action"] == "force_exit"
    req = conn.execute("SELECT * FROM exit_requests").fetchone()
    assert req["reason"] == reason and req["msg_id"] == 500
    assert req["ft_status"] == 200 and req["answered_at"] is not None

    exits = _attribute(conn, pos_id, _trade([_order("mkt-1", _ms())]))
    assert [(e["order_id"], e["reason"], e["source"]) for e in exits] == [
        ("mkt-1", reason, "receiver_request")]


def test_posted_sl_exit_is_attributed():
    cfg, conn, pos_id = _setup(posted_sl=True)

    async def mark(_symbol, session=None):
        return 0.085  # below the 0.09 posted stop

    with patch.object(receiver_main, "get_execution_mark_price", side_effect=mark), \
         patch.object(receiver_main, "ft_force_exit", side_effect=_ok_exit), \
         patch.object(receiver_main, "_notify_telegram"):
        _run(receiver_main._check_posted_sl(cfg, conn))
    req = conn.execute("SELECT * FROM exit_requests").fetchone()
    assert req["reason"] == "posted_sl" and req["msg_id"] is None

    exits = _attribute(conn, pos_id, _trade([_order("mkt-sl", _ms())]))
    assert exits[0]["reason"] == "posted_sl"


def test_signal_update_close_is_attributed():
    _cfg, conn, pos_id = _setup()
    with patch.object(receiver_main, "ft_force_exit", side_effect=_ok_exit):
        r = _run(receiver_main._process_event(_close_payload(
            "signal_update", 600, instruction="close_full_now")))
    assert r["action"] == "force_exit"
    exits = _attribute(conn, pos_id, _trade([_order("mkt-su", _ms())]))
    assert exits[0]["reason"] == "signal_update_close"


def test_signal_update_tp1_limit_is_attributed_by_order_id():
    _cfg, conn, pos_id = _setup(active_tp=True)
    posted = {}

    async def get_trade(_cfg, trade_id, session=None):
        orders = []
        if posted:
            orders = [_order("tp1-consolidated", _ms(), status="open",
                             price=0.105) | {"is_open": True}]
        return {"trade_id": 9, "pair": "POL/USDC:USDC", "is_short": False,
                "is_open": True, "amount": 10.0, "current_rate": 0.101,
                "orders": orders}

    async def post_limit(_cfg, trade_id, amount, price, session=None):
        posted["price"] = price
        return {"status": 200, "body": '{"result":"Created exit order"}'}

    async def cancel(_cfg, trade_id, session=None):
        return {"status": 200, "body": "{}"}

    with patch.object(receiver_main, "ft_get_trade", side_effect=get_trade), \
         patch.object(receiver_main, "ft_force_exit_limit", side_effect=post_limit), \
         patch.object(receiver_main, "ft_cancel_open_order", side_effect=cancel):
        r = _run(receiver_main._process_event(_close_payload(
            "signal_update", 700, instruction="close_at_target_1")))
    assert r["action"] == "limit_posted"
    req = conn.execute("SELECT * FROM exit_requests").fetchone()
    assert req["reason"] == "signal_update_tp1"
    assert req["ft_order_id"] == "tp1-consolidated"
    # The consolidated limit is ALSO a target_orders row; the request wins.
    exits = _attribute(conn, pos_id, _trade([_order("tp1-consolidated", _ms(),
                                                    price=0.105)]))
    assert exits[0]["reason"] == "signal_update_tp1"


def test_tp_ladder_fill_is_labelled_on_the_posted_ladder():
    """Legacy idx indexes the targets still ahead at entry. Two were crossed
    here, so idx 0 is the signal's TP3; the tick-rounded price still maps."""
    _cfg, conn, pos_id = _setup()
    conn.execute("INSERT INTO target_orders (pos_id, idx, price, amount, state, "
                 "ft_order_id) VALUES (?, 0, 0.11751, 10, 'filled', 'tp-a')", (pos_id,))
    exits = _attribute(conn, pos_id, _trade([_order("tp-a", _ms(), price=0.11751)]))
    assert (exits[0]["reason"], exits[0]["source"]) == ("tp3", "receiver_tp_ladder")


def test_tp_submission_whose_order_id_was_never_seen_matches_by_window():
    _cfg, conn, pos_id = _setup()
    conn.execute("INSERT INTO target_orders (pos_id, idx, price, amount, state, "
                 "submitted_at) VALUES (?, 0, 0.105, 10, 'unknown', ?)",
                 (pos_id, datetime.now(timezone.utc).isoformat()))
    exits = _attribute(conn, pos_id, _trade([_order("tp-x", _ms(), price=0.105)]))
    assert exits[0]["reason"] == "tp1"


# ── orders the receiver did not submit ─────────────────────────────────────


def test_force_exit_outside_the_receiver_is_manual():
    """The 2026-09-18 DOT/FIL/LINK shape: a force_exit with no receiver
    record is labelled manual, explicitly."""
    _cfg, conn, pos_id = _setup()
    exits = _attribute(conn, pos_id, _trade([_order("op-1", _ms())]))
    assert (exits[0]["reason"], exits[0]["source"]) == ("manual", "outside_receiver")


def test_order_older_than_the_ledger_is_unattributed_not_manual():
    _cfg, conn, pos_id = _setup()
    old = _ms(datetime.now(timezone.utc) - timedelta(days=3))
    exits = _attribute(conn, pos_id, _trade([_order("old-1", old)]))
    assert (exits[0]["reason"], exits[0]["source"]) == ("unattributed", "before_exit_ledger")


def test_freqtrade_native_stop_and_tags_keep_their_own_reason():
    _cfg, conn, pos_id = _setup()
    exits = _attribute(conn, pos_id, _trade([
        _order("sl-1", _ms(), side="stoploss", tag=None),
        _order("liq-1", _ms(), tag="liquidation"),
    ]))
    assert {e["order_id"]: e["reason"] for e in exits} == {
        "sl-1": "stoploss_on_exchange", "liq-1": "liquidation"}


def test_manual_close_after_a_receiver_exit_window_stays_manual():
    _cfg, conn, pos_id = _setup()
    with patch.object(receiver_main, "ft_force_exit", side_effect=_ok_exit):
        _run(receiver_main._process_event(_close_payload("close_partial", 800, pct=50)))
    now = datetime.now(timezone.utc)
    exits = _attribute(conn, pos_id, _trade([
        _order("mkt-1", _ms(now)),
        _order("op-2", _ms(now + timedelta(minutes=10))),
    ]))
    assert [e["reason"] for e in exits] == ["channel_partial_close", "manual"]


def test_a_receiver_request_claims_one_order_only():
    _cfg, conn, pos_id = _setup()
    with patch.object(receiver_main, "ft_force_exit", side_effect=_ok_exit):
        _run(receiver_main._process_event(_close_payload("close_full", 900)))
    now = _ms()
    exits = _attribute(conn, pos_id, _trade([_order("a", now), _order("b", now + 1)]))
    assert [e["reason"] for e in exits] == ["channel_close", "manual"]


def test_rejected_request_does_not_outrank_an_accepted_one():
    _cfg, conn, pos_id = _setup()
    now = datetime.now(timezone.utc).isoformat()
    for reason, status in (("channel_partial_close", 502), ("posted_sl", 200)):
        conn.execute(
            "INSERT INTO exit_requests (pos_id, ft_trade_id, reason, ordertype, "
            "submitted_at, answered_at, ft_status) VALUES (?, 9, ?, 'market', ?, ?, ?)",
            (pos_id, reason, now, now, status))
    exits = _attribute(conn, pos_id, _trade([_order("m", _ms())]))
    assert exits[0]["reason"] == "posted_sl"


# ── endpoint ───────────────────────────────────────────────────────────────


def test_exits_endpoint_joins_the_right_position_by_pair():
    _cfg, conn, pos_id = _setup()
    # Same ft_trade_id from an older Freqtrade DB epoch, different pair.
    conn.execute(
        "INSERT INTO positions (signal_id, symbol, pair, direction, state, "
        " open_msg_id, open_date, ft_trade_id) VALUES (1, 'ONDO', "
        " 'ONDO/USDC:USDC', 'long', 'closed', 50, '2026-08-01T00:00:00+00:00', 9)")
    conn.execute("INSERT INTO target_orders (pos_id, idx, price, amount, state, "
                 "ft_order_id) VALUES (?, 0, 0.105, 10, 'filled', 'tp-a')", (pos_id,))

    async def get_trade(_cfg, trade_id, session=None):
        return _trade([_order("tp-a", _ms(), price=0.105), _order("op", _ms())])

    with patch.object(receiver_main, "ft_get_trade", side_effect=get_trade):
        body = _run(receiver_main.exit_attribution(9))
    assert body["pos_id"] == pos_id
    assert body["ft_exit_reason"] == "force_exit"
    assert [(e["order_id"], e["reason"]) for e in body["exits"]] == [
        ("tp-a", "tp1"), ("op", "manual")]


def test_ledger_start_is_set_once():
    _cfg, conn, _pos_id = _setup()
    first = conn.execute("SELECT value FROM receiver_meta").fetchone()[0]
    receiver_main.init_db(os.environ["KILLERS_DB"])
    again = conn.execute("SELECT value FROM receiver_meta").fetchone()[0]
    assert first == again
