#!/usr/bin/env python3
"""Mark Killers stop-order rows Hyperliquid already removed as canceled (#148).

Operator-run, one-off. When the last TP limit closes a Killers trade,
Hyperliquid drops the reduce-only stop with the position. Freqtrade 2026.7
then fails to cancel it ("Order was never placed, already canceled, or
filled"), logs `Could not cancel stoploss order`, and leaves the DB row
`status='open'`, `ft_is_open=1` until its next restart re-reads it
(startup_update_open_orders). This script writes the state that restart
would: `status='canceled'`, `ft_is_open=0`, `order_update_date=now`.

It runs INSIDE ft-killers-scalp as ftuser (the DB is in WAL mode; a root
writer would leave root-owned -wal/-shm files Freqtrade cannot open), via
deploy/vps/close-orphan-killers-stoploss-rows.sh. It:
  - lists stop rows still open on CLOSED trades (dry run by default);
  - refuses if any of their order ids is open on Hyperliquid, or if the
    exchange cannot be read (the account address comes from the container's
    environment and is never printed);
  - with --yes and the row ids you reviewed, backs the DB up next to it under
    user_data/backups/ and updates exactly those rows in one transaction.
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = "/freqtrade/user_data/tradesv3.live.KillersScalpV1.hyperliquid.sqlite"
DEFAULT_INFO_URL = "http://hl-gateway:8080/killers/info"
ADDRESS_ENV = "FREQTRADE__EXCHANGE__WALLET_ADDRESS"


def orphan_stop_rows(conn):
    """Stop orders Freqtrade still marks open although their trade closed."""
    return conn.execute(
        "SELECT o.id, o.ft_trade_id, o.ft_pair, o.order_id, o.status, "
        "       o.order_update_date, t.close_date "
        "FROM orders o JOIN trades t ON t.id = o.ft_trade_id "
        "WHERE o.ft_order_side = 'stoploss' AND o.ft_is_open = 1 AND t.is_open = 0 "
        "ORDER BY o.id"
    ).fetchall()


def exchange_open_order_ids(info_url, address, opener=urllib.request.urlopen):
    """Order ids currently open on Hyperliquid for this account (read-only)."""
    request = urllib.request.Request(
        info_url,
        data=json.dumps({"type": "frontendOpenOrders", "user": address}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with opener(request, timeout=15) as response:
        orders = json.load(response)
    if not isinstance(orders, list):
        raise ValueError(f"unexpected open-orders response type {type(orders).__name__}")
    return {str(o.get("oid")) for o in orders if isinstance(o, dict) and o.get("oid") is not None}


def backup(db_path, stamp):
    target_dir = Path(db_path).parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{Path(db_path).stem}.before-orphan-sl-{stamp}.sqlite"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def close_rows(conn, row_ids, now):
    """Update exactly `row_ids`, re-checking the orphan condition in the WHERE."""
    placeholders = ",".join("?" for _ in row_ids)
    conn.execute("BEGIN IMMEDIATE")
    try:
        changed = conn.execute(
            f"UPDATE orders SET status = 'canceled', ft_is_open = 0, order_update_date = ? "
            f"WHERE id IN ({placeholders}) AND ft_order_side = 'stoploss' AND ft_is_open = 1 "
            f"AND ft_trade_id IN (SELECT id FROM trades WHERE is_open = 0)",
            (now, *row_ids),
        ).rowcount
        if changed != len(row_ids):
            raise RuntimeError(f"expected {len(row_ids)} rows, would change {changed}; rolled back")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return changed


def main(argv=None, *, environ=None, open_orders=exchange_open_order_ids, clock=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--info-url", default=DEFAULT_INFO_URL)
    parser.add_argument("--yes", action="store_true",
                        help="apply; requires the reviewed order row ids")
    parser.add_argument("row_ids", nargs="*", type=int,
                        help="orders.id values to close (from the dry run)")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = orphan_stop_rows(conn)
    if not rows:
        print("no open stop rows on closed trades; nothing to do")
        return 0
    print("open stop rows on closed trades:")
    for row in rows:
        print(f"  row {row['id']}: trade {row['ft_trade_id']} {row['ft_pair']} "
              f"status={row['status']} last_update={row['order_update_date']} "
              f"trade_closed={row['close_date']}")

    address = environ.get(ADDRESS_ENV, "").strip()
    if not address:
        print(f"REFUSING: {ADDRESS_ENV} is not set; cannot check the exchange")
        return 1
    try:
        live = open_orders(args.info_url, address)
    except Exception as exc:  # noqa: BLE001 — any read failure is a refusal
        print(f"REFUSING: could not read open orders from Hyperliquid: {exc}")
        return 1
    still_open = [row["id"] for row in rows if str(row["order_id"]) in live]
    if still_open:
        print(f"REFUSING: rows {still_open} are still open on Hyperliquid; leave them to Freqtrade")
        return 1
    print(f"none of these {len(rows)} orders is open on Hyperliquid ({len(live)} open orders checked)")

    if not args.yes:
        ids = " ".join(str(row["id"]) for row in rows)
        print(f"dry run only. To apply: --yes {ids}")
        return 0
    wanted = sorted(set(args.row_ids))
    candidates = {row["id"] for row in rows}
    if not wanted:
        print("REFUSING: --yes needs the row ids you reviewed")
        return 1
    if not set(wanted) <= candidates:
        print(f"REFUSING: rows {sorted(set(wanted) - candidates)} are not open stop rows on closed trades")
        return 1

    now = clock() if clock else datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    target = backup(args.db, stamp)
    print(f"backup written: {target}")
    changed = close_rows(conn, wanted, now.strftime("%Y-%m-%d %H:%M:%S.%f"))
    print(f"marked {changed} rows canceled: {wanted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
