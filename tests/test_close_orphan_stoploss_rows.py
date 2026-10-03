"""#148: operator script that closes Freqtrade stop rows Hyperliquid dropped.

Synthetic DB with the Freqtrade columns the script touches. The exchange
read is injected; the script itself refuses when it cannot read it.
"""
import importlib.util
import io
import json
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "close_orphan_stoploss_rows", ROOT / "deploy/vps/close_orphan_stoploss_rows.py")
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

ENV = {script.ADDRESS_ENV: "0xnot-a-real-account"}
NOW = datetime(2026, 10, 3, 1, 2, 3, 456789, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "tradesv3.live.KillersScalpV1.hyperliquid.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT, is_open BOOLEAN,
                             close_date DATETIME);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, ft_trade_id INTEGER,
            ft_order_side TEXT, ft_pair TEXT, ft_is_open BOOLEAN, order_id TEXT,
            status TEXT, order_update_date DATETIME);
        -- closed POL trade: stop row left open (the #148 shape)
        INSERT INTO trades VALUES (9, 'POL/USDC:USDC', 0, '2026-09-24 14:16:40');
        INSERT INTO orders VALUES (35, 9, 'sell', 'POL/USDC:USDC', 0, 'tp-9', 'closed', NULL);
        INSERT INTO orders VALUES (36, 9, 'stoploss', 'POL/USDC:USDC', 1, 'sl-9', 'open',
                                   '2026-09-24 14:16:32.923340');
        -- closed RENDER trade, same shape
        INSERT INTO trades VALUES (11, 'RENDER/USDC:USDC', 0, '2026-09-27 00:11:37');
        INSERT INTO orders VALUES (42, 11, 'stoploss', 'RENDER/USDC:USDC', 1, 'sl-11', 'open', NULL);
        -- OPEN trade: its live stop must never be touched
        INSERT INTO trades VALUES (13, 'XRP/USDC:USDC', 1, NULL);
        INSERT INTO orders VALUES (48, 13, 'stoploss', 'XRP/USDC:USDC', 1, 'sl-13', 'open', NULL);
        -- closed trade whose stop Freqtrade already healed
        INSERT INTO trades VALUES (2, 'GRAM/USDC:USDC', 0, '2026-09-21 09:40:44');
        INSERT INTO orders VALUES (6, 2, 'stoploss', 'GRAM/USDC:USDC', 0, 'sl-2', 'canceled', NULL);
    """)
    conn.commit()
    conn.close()
    return path


def _run(db, *args, live=("sl-13",), environ=ENV):
    def open_orders(_url, address):
        assert address == ENV[script.ADDRESS_ENV]
        return set(live)

    out = io.StringIO()
    with redirect_stdout(out):
        code = script.main(["--db", str(db), *args], environ=environ,
                           open_orders=open_orders, clock=lambda: NOW)
    return code, out.getvalue()


def _row(db, row_id):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM orders WHERE id=?", (row_id,)).fetchone())
    finally:
        conn.close()


def test_dry_run_lists_only_open_stops_on_closed_trades_and_changes_nothing(db):
    code, out = _run(db)
    assert code == 0
    assert "row 36: trade 9 POL" in out and "row 42: trade 11 RENDER" in out
    assert "row 48" not in out and "row 6:" not in out
    assert "--yes 36 42" in out
    assert _row(db, 36)["ft_is_open"] == 1
    assert not (db.parent / "backups").exists()


def test_apply_marks_exactly_the_reviewed_rows_and_backs_up_first(db):
    code, out = _run(db, "--yes", "36", "42")
    assert code == 0, out
    for row_id in (36, 42):
        row = _row(db, row_id)
        assert (row["status"], row["ft_is_open"]) == ("canceled", 0)
        assert row["order_update_date"] == "2026-10-03 01:02:03.456789"
    live_stop = _row(db, 48)
    assert (live_stop["status"], live_stop["ft_is_open"]) == ("open", 1)
    backups = list((db.parent / "backups").glob("*.sqlite"))
    assert len(backups) == 1
    saved = sqlite3.connect(backups[0])
    assert saved.execute("SELECT ft_is_open FROM orders WHERE id=36").fetchone()[0] == 1
    saved.close()


def test_apply_can_target_a_subset(db):
    code, _out = _run(db, "--yes", "36")
    assert code == 0
    assert _row(db, 36)["ft_is_open"] == 0 and _row(db, 42)["ft_is_open"] == 1


def test_refuses_when_an_order_is_still_open_on_the_exchange(db):
    code, out = _run(db, "--yes", "36", "42", live=("sl-9", "sl-13"))
    assert code == 1 and "rows [36] are still open on Hyperliquid" in out
    assert _row(db, 36)["ft_is_open"] == 1


def test_refuses_when_the_exchange_cannot_be_read(db):
    def broken(_url, _address):
        raise OSError("gateway down")

    out = io.StringIO()
    with redirect_stdout(out):
        code = script.main(["--db", str(db), "--yes", "36"], environ=ENV,
                           open_orders=broken, clock=lambda: NOW)
    assert code == 1 and "could not read open orders" in out.getvalue()
    assert _row(db, 36)["ft_is_open"] == 1


def test_refuses_without_the_account_address(db):
    code, out = _run(db, "--yes", "36", environ={})
    assert code == 1 and "is not set" in out
    assert _row(db, 36)["ft_is_open"] == 1


def test_refuses_rows_that_are_not_orphans(db):
    code, out = _run(db, "--yes", "36", "48")
    assert code == 1 and "rows [48] are not open stop rows on closed trades" in out
    assert _row(db, 36)["ft_is_open"] == 1 and _row(db, 48)["ft_is_open"] == 1


def test_yes_without_row_ids_refuses(db):
    code, out = _run(db, "--yes")
    assert code == 1 and "needs the row ids" in out


def test_nothing_to_do_on_a_clean_db(db):
    _run(db, "--yes", "36", "42")
    code, out = _run(db)
    assert code == 0 and "nothing to do" in out


def test_exchange_reader_parses_hyperliquid_open_orders():
    payload = [{"coin": "XRP", "oid": 561441602146}, {"coin": "ICP", "oid": 562544831935}]

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    seen = {}

    def opener(request, timeout):
        seen["body"] = json.loads(request.data)
        return _Resp(json.dumps(payload).encode())

    ids = script.exchange_open_order_ids("http://gw/killers/info", "0xabc", opener=opener)
    assert ids == {"561441602146", "562544831935"}
    assert seen["body"] == {"type": "frontendOpenOrders", "user": "0xabc"}
