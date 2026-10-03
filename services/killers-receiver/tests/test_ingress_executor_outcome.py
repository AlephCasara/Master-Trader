"""#125: the ingress audit records what Freqtrade actually did.

The 2026-09-30 audit found six DOT partial closes recorded as
`final_action=force_exit` while Freqtrade had refused every one (five 502
"Remaining amount ... would be too small", one 500). The receiver now reports
`exit_rejected`, keeps `final_status` as the HTTP status it returned to the
observer (200: the observer retries 5xx), and stores Freqtrade's status and
error in `executor_status` / `executor_error`. Freqtrade is mocked.
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main as receiver_main  # noqa: E402

# Body shape copied from a live Freqtrade 502 (numbers only).
TOO_SMALL = ('{"error":"Error querying /api/v1/forceexit: Remaining amount '
             'of 8.138365 would be too small."}')
TOO_SMALL_MSG = ("Error querying /api/v1/forceexit: Remaining amount of "
                 "8.138365 would be too small.")


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
        self.entry_lock = asyncio.Lock()


def _setup():
    tf = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    os.environ["KILLERS_DB"] = tf.name
    os.environ["KILLERS_ACTIVE_TP_LIMITS"] = "false"
    cfg = receiver_main.Config()
    cfg.notify_url = ""
    conn = receiver_main.init_db(cfg.db_path)
    conn.execute(
        "INSERT INTO positions (signal_id, symbol, pair, direction, state, "
        " open_msg_id, open_date, stake_usd, leverage, ft_trade_id, pct_open) "
        "VALUES (3001, 'DOT', 'DOT/USDC:USDC', 'long', 'open', 100, "
        " '2026-09-18T00:00:00+00:00', 20.0, 3.0, 42, 100)")
    receiver_main.app.state = _FakeState(conn, cfg)
    return cfg, conn


def _payload(kind, msg_id, **extra):
    cls = {"kind": kind, "signal_id": 3001, "symbol": "DOT", "direction": "long"}
    cls.update(extra)
    return receiver_main.EventPayload(
        msg={"id": msg_id, "date": "2026-09-18T01:00:00+00:00", "text": "x"},
        classification=cls,
    )


def _deliver(payload, ft_response):
    async def fake_exit(_cfg, trade_id, pct=None, session=None, **_kw):
        return ft_response

    with patch.object(receiver_main, "ft_force_exit", side_effect=fake_exit):
        return _run(receiver_main.handle_event(payload))


def _ingress(conn, msg_id):
    return conn.execute("SELECT * FROM ingress_events WHERE msg_id=?",
                        (msg_id,)).fetchone()


def _revision(conn, msg_id):
    return conn.execute("SELECT * FROM ingress_revisions WHERE msg_id=? "
                        "ORDER BY rev_id DESC LIMIT 1", (msg_id,)).fetchone()


def test_partial_exit_refused_with_502_is_recorded_as_exit_rejected():
    _cfg, conn = _setup()
    result = _deliver(_payload("close_partial", 3862, pct=50),
                      {"status": 502, "body": TOO_SMALL})
    assert result["action"] == "exit_rejected"
    for row in (_ingress(conn, 3862), _revision(conn, 3862)):
        assert row["final_action"] == "exit_rejected"
        assert row["final_status"] == 200  # what the observer got back
        assert row["executor_status"] == 502
        assert row["executor_error"] == TOO_SMALL_MSG
    pos = conn.execute("SELECT state, pct_open FROM positions").fetchone()
    assert pos["state"] == "open" and pos["pct_open"] == 100


def test_partial_exit_refused_with_plain_500_keeps_the_raw_error():
    _cfg, conn = _setup()
    result = _deliver(_payload("close_partial", 3928, pct=50),
                      {"status": 500, "body": "Internal Server Error"})
    assert result["action"] == "exit_rejected"
    row = _ingress(conn, 3928)
    assert row["final_action"] == "exit_rejected"
    assert row["executor_status"] == 500
    assert row["executor_error"] == "Internal Server Error"


def test_accepted_exit_is_still_force_exit_with_executor_status():
    _cfg, conn = _setup()
    result = _deliver(_payload("close_full", 3950),
                      {"status": 200, "body": '{"result":"Created exit order"}'})
    assert result["action"] == "force_exit"
    row = _ingress(conn, 3950)
    assert row["final_action"] == "force_exit"
    assert row["executor_status"] == 200
    assert row["executor_error"] is None


def test_delivery_without_a_freqtrade_call_leaves_executor_columns_null():
    _cfg, conn = _setup()
    result = _run(receiver_main.handle_event(_payload("chat", 3960)))
    assert result["action"] == "ignored"
    row = _ingress(conn, 3960)
    assert row["executor_status"] is None and row["executor_error"] is None


def test_rejected_entry_is_recorded_as_entry_rejected():
    cfg, conn = _setup()
    conn.execute("DELETE FROM positions")
    # Production-like sizing so the entry clears the venue minimum and
    # actually reaches Freqtrade.
    cfg.risk_usd, cfg.max_margin_usd = 12.0, 40.0

    async def mark(_symbol, session=None):
        return 1.0

    async def enter(*_a, **_kw):
        return {"status": 502, "body": '{"error":"stake below minimum"}'}

    payload = receiver_main.EventPayload(
        msg={"id": 3970, "date": "2026-09-18T01:00:00+00:00",
             "text": "TARGETS: 1.05 - 1.10"},
        classification={"kind": "open", "signal_id": 3002, "symbol": "DOT",
                        "direction": "long", "entry_range": [0.99, 1.01],
                        "sl": 0.90},
    )
    with patch.object(receiver_main, "get_execution_mark_price", side_effect=mark), \
         patch.object(receiver_main, "ft_force_enter", side_effect=enter):
        result = _run(receiver_main.handle_event(payload))
    assert result["action"] == "entry_rejected"
    row = _ingress(conn, 3970)
    assert row["final_action"] == "entry_rejected"
    assert row["executor_status"] == 502
    assert row["executor_error"] == "stake below minimum"
    assert conn.execute("SELECT state FROM positions").fetchone()["state"] == "failed"


def _deliver_tp1_fallback(conn, market_status):
    """signal_update close_at_target_1: both TP1 limit posts fail, then the
    receiver falls back to a full market close."""
    conn.execute("INSERT INTO target_orders (pos_id, idx, price, amount, state) "
                 "SELECT pos_id, 0, 1.20, 10, 'pending' FROM positions")

    async def get_trade(_cfg, trade_id, session=None):
        return {"trade_id": 42, "is_short": False, "is_open": True,
                "amount": 10.0, "current_rate": 1.0, "orders": []}

    async def cancel(_cfg, trade_id, session=None):
        return {"status": 200, "body": "{}"}

    async def post_limit(_cfg, trade_id, amount, price, session=None):
        return {"status": 502, "body": '{"error":"limit refused"}'}

    async def market(_cfg, trade_id, pct=None, session=None, **_kw):
        if market_status == 200:
            return {"status": 200, "body": '{"result":"Created exit order"}'}
        return {"status": market_status, "body": '{"error":"market refused"}'}

    async def no_sleep(_s):
        return None

    payload = _payload("signal_update", 3990, instruction="close_at_target_1")
    with patch.object(receiver_main, "ft_get_trade", side_effect=get_trade), \
         patch.object(receiver_main, "ft_cancel_open_order", side_effect=cancel), \
         patch.object(receiver_main, "ft_force_exit_limit", side_effect=post_limit), \
         patch.object(receiver_main, "ft_force_exit", side_effect=market), \
         patch.object(receiver_main.asyncio, "sleep", side_effect=no_sleep):
        return _run(receiver_main.handle_event(payload))


def test_tp1_fallback_records_the_market_close_that_closed_the_position():
    """Review finding: the result's `ft` was the failed limit post, so the
    ingress row reported an executor failure for a position that closed."""
    _cfg, conn = _setup()
    result = _deliver_tp1_fallback(conn, market_status=200)
    assert result["action"] == "limit_failed_market_closed"
    row = _ingress(conn, 3990)
    assert row["final_action"] == "limit_failed_market_closed"
    assert row["executor_status"] == 200
    assert row["executor_error"] is None
    assert conn.execute("SELECT state FROM positions").fetchone()["state"] == "closed"


def test_tp1_fallback_failure_records_the_market_close_error():
    _cfg, conn = _setup()
    result = _deliver_tp1_fallback(conn, market_status=500)
    assert result["action"] == "UNCOVERED_POSITION_ALERT"
    row = _ingress(conn, 3990)
    assert row["executor_status"] == 500
    assert row["executor_error"] == "market refused"


def test_rejected_exit_alert_says_rejected_not_closed():
    cfg, _conn = _setup()
    text = receiver_main._format_event_summary(
        cfg, _payload("close_partial", 3862),
        {"action": "exit_rejected", "pos_id": 3, "kind": "close_partial",
         "ft": {"status": 502, "body": TOO_SMALL},
         "pct_closed_of_original": 0.0, "pct_open_after": 100.0})
    assert text.startswith("❌")
    assert "REJECTED" in text and "ft_status=502" in text
    assert "would be too small" in text
    assert "closed" not in text.split("REJECTED")[1].split("—")[0]


def test_migration_adds_columns_without_rewriting_existing_rows():
    tf = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    legacy = sqlite3.connect(tf.name)
    legacy.executescript("""
        CREATE TABLE ingress_events (
            ingress_id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id INTEGER NOT NULL UNIQUE, received_at TEXT NOT NULL,
            msg_date TEXT, kind TEXT, symbol TEXT, signal_id INTEGER,
            raw_payload TEXT NOT NULL, final_action TEXT,
            final_status INTEGER, completed_at TEXT);
        CREATE TABLE ingress_revisions (
            rev_id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER NOT NULL,
            received_at TEXT NOT NULL, msg_edit_date TEXT, kind TEXT,
            symbol TEXT, signal_id INTEGER, raw_payload TEXT NOT NULL,
            final_action TEXT, final_status INTEGER, completed_at TEXT);
        INSERT INTO ingress_events (msg_id, received_at, kind, raw_payload,
            final_action, final_status)
        VALUES (3862, '2026-09-18T01:00:00+00:00', 'close_partial', '{}',
                'force_exit', 200);
    """)
    legacy.commit()
    legacy.close()

    conn = receiver_main.init_db(tf.name)
    row = conn.execute("SELECT * FROM ingress_events WHERE msg_id=3862").fetchone()
    assert row["final_action"] == "force_exit"  # history is not rewritten
    assert row["executor_status"] is None and row["executor_error"] is None
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ingress_revisions)")}
    assert {"executor_status", "executor_error"} <= cols
    # Idempotent on the next start.
    receiver_main.init_db(tf.name)


def test_executor_outcome_ignores_results_without_a_freqtrade_response():
    assert receiver_main._executor_outcome({"action": "skipped"}) == (None, None)
    assert receiver_main._executor_outcome({"ft": {"status": "pending"}}) == (None, None)
    assert receiver_main._executor_outcome(None) == (None, None)
    assert receiver_main._executor_outcome(
        {"ft": {"status": 0, "body": "cannot fetch trade 42 for partial exit"}}
    ) == (0, "cannot fetch trade 42 for partial exit")
    assert json.loads(TOO_SMALL)["error"] == TOO_SMALL_MSG
