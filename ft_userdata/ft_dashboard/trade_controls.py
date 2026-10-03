"""Human-confirmed position actions. No automatic retries or background exits.

Two actions share one guard path: a full-position market close, and the
cancellation of a resting entry order. Each requires the dashboard origin,
the bot's API credentials, a fresh /status snapshot that still matches what
the operator confirmed, and a durable reservation before anything is sent.
"""
import base64
import hmac
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


class ClosePosition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    pair: str = Field(min_length=1, max_length=80)
    amount: float = Field(gt=0, allow_inf_nan=False)
    open_timestamp: float = Field(gt=0, allow_inf_nan=False)
    is_short: bool


class CancelEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    pair: str = Field(min_length=1, max_length=80)
    order_id: str = Field(min_length=1, max_length=200)
    amount: float = Field(ge=0, le=0, allow_inf_nan=False)  # only an entry with nothing filled
    open_timestamp: float = Field(gt=0, allow_inf_nan=False)
    is_short: bool


def connect():
    path = Path(os.environ.get('DASHBOARD_ACTION_DB', '/var/lib/dashboard-actions/actions.sqlite'))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=5)
    path.chmod(0o600)
    db.execute('CREATE TABLE IF NOT EXISTS actions (request_id TEXT PRIMARY KEY, bot TEXT, trade INTEGER, payload TEXT, opened REAL, state TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP, kind TEXT NOT NULL DEFAULT \'close\')')
    if 'kind' not in {row[1] for row in db.execute('PRAGMA table_info(actions)')}:
        # Rows written before cancel-entry existed were all full closes.
        db.execute('ALTER TABLE actions ADD COLUMN kind TEXT NOT NULL DEFAULT \'close\'')
    db.commit()
    return db


def _open_entry_orders(trade):
    entry_side = 'sell' if trade.get('is_short') else 'buy'
    return [o for o in trade.get('orders', []) if o.get('is_open') and o.get('ft_order_side') == entry_side]


def _nothing_filled(trade):
    """Trade amount 0 and every entry order reports filled 0. Missing data is not zero."""
    entry_side = 'sell' if trade.get('is_short') else 'buy'
    try:
        return float(trade['amount']) == 0 and all(
            float(o['filled']) == 0 for o in trade.get('orders', []) if o.get('ft_order_side') == entry_side)
    except (KeyError, TypeError, ValueError):
        return False


def _entry_cancel_confirmed(trade, order_id):
    """After the DELETE: the trade is gone, or the order no longer rests and nothing filled."""
    if trade is None:
        return True  # Freqtrade removes a trade whose entry was cancelled before any fill.
    if _open_entry_orders(trade) or not _nothing_filled(trade):
        return False
    order = next((o for o in trade.get('orders', []) if o.get('order_id') == order_id), None)
    return order is None or str(order.get('status') or '').lower() in {'canceled', 'cancelled', 'expired', 'rejected'}


def install(app, bots, auth):
    def authorize(bot_key, trade_id, request, action):
        bot = next((b for b in bots if b['key'] == bot_key), None)
        if bot is None or trade_id <= 0:
            raise HTTPException(404, 'Unknown bot or position')
        origin = urlsplit(request.headers.get('origin', ''))
        if (origin.scheme != 'https' or origin.netloc != request.headers.get('host')
                or request.headers.get('x-trade-action') != action
                or request.headers.get('sec-fetch-site', 'same-origin') != 'same-origin'):
            raise HTTPException(403, 'Use the dashboard on its HTTPS address')
        expected_user, expected_password = auth(bot['url'])
        if not expected_password or expected_password == 'mastertrader':
            raise HTTPException(503, 'Trading controls require configured bot credentials')
        try:
            kind, encoded = request.headers.get('authorization', '').split(' ', 1)
            user, password = base64.b64decode(encoded, validate=True).decode().split(':', 1)
            authenticated = (kind.lower() == 'basic'
                             and hmac.compare_digest(user.encode(), expected_user.encode())
                             and hmac.compare_digest(password.encode(), expected_password.encode()))
        except (ValueError, UnicodeError):
            authenticated = False
        if not authenticated:
            raise HTTPException(401, 'Bot API username or password is incorrect')
        return bot, (expected_user, expected_password)

    def replay(bot_key, trade_id, payload):
        with connect() as db:
            previous = db.execute('SELECT bot,trade,payload,state FROM actions WHERE request_id=?', (str(payload.request_id),)).fetchone()
        if previous:
            if previous[:3] != (bot_key, trade_id, payload.model_dump_json()):
                raise HTTPException(409, 'Request identity was already used for another action')
            return {'state': previous[3], 'replayed': True}
        return None

    async def snapshot(client, bot, trade_id, no_action):
        try:
            response = await client.get(bot['url'].rstrip('/') + '/api/v1/status')
            response.raise_for_status()
            trades = response.json()
            if not isinstance(trades, list):
                raise ValueError('Invalid snapshot')
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, f'Cannot refresh position; {no_action}') from None
        return next((t for t in trades if t.get('trade_id') == trade_id), None)

    def reserve(bot_key, trade_id, payload, kind, conflict):
        # Durable reservation before the request. A timeout or process death never permits a blind retry.
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT state FROM actions WHERE bot=? AND trade=? AND opened=? AND kind=? AND state IN (\'submitting\',\'accepted\',\'unknown\')', (bot_key, trade_id, payload.open_timestamp, kind)).fetchone()
            if old:
                raise HTTPException(409, conflict)
            try:
                db.execute('INSERT INTO actions(request_id,bot,trade,payload,opened,state,kind) VALUES(?,?,?,?,?,?,?)', (str(payload.request_id), bot_key, trade_id, payload.model_dump_json(), payload.open_timestamp, 'submitting', kind))
            except sqlite3.IntegrityError:
                raise HTTPException(409, 'Request already submitted') from None

    async def submit(payload, send):
        try:
            result = await send()
            # Non-2xx may follow a side effect: treat as uncertain, not safely retryable.
            state = 'accepted' if result.is_success else 'unknown'
        except httpx.HTTPError:
            state = 'unknown'
        with connect() as db:
            db.execute('UPDATE actions SET state=? WHERE request_id=?', (state, str(payload.request_id)))
        return {'state': state, 'replayed': False}

    @app.post('/api/trades/{bot_key}/{trade_id}/close')
    async def close_position(bot_key: str, trade_id: int, payload: ClosePosition, request: Request):
        bot, credentials = authorize(bot_key, trade_id, request, 'close')
        if (previous := replay(bot_key, trade_id, payload)):
            return previous
        async with httpx.AsyncClient(timeout=45, auth=credentials) as client:
            trade = await snapshot(client, bot, trade_id, 'no exit submitted')
            if not trade:
                return {'state': 'already_closed'}
            if (trade.get('pair') != payload.pair or bool(trade.get('is_short')) != payload.is_short
                    or trade.get('amount') != payload.amount
                    or trade.get('open_timestamp') != payload.open_timestamp):
                raise HTTPException(409, 'Position changed. Refresh and review it again; no exit submitted')
            if _open_entry_orders(trade):
                raise HTTPException(409, 'An entry order is still open. Resolve it before closing this position')
            reserve(bot_key, trade_id, payload, 'close', 'An exit request already exists. Check current orders before another action')
            return await submit(payload, lambda: client.post(bot['url'].rstrip('/') + '/api/v1/forceexit', json={'tradeid': str(trade_id), 'ordertype': 'market'}))

    @app.post('/api/trades/{bot_key}/{trade_id}/cancel-entry')
    async def cancel_entry(bot_key: str, trade_id: int, payload: CancelEntry, request: Request):
        bot, credentials = authorize(bot_key, trade_id, request, 'cancel-entry')
        if (previous := replay(bot_key, trade_id, payload)):
            return previous
        async with httpx.AsyncClient(timeout=45, auth=credentials) as client:
            trade = await snapshot(client, bot, trade_id, 'no cancel submitted')
            if not trade:
                return {'state': 'already_closed'}
            if (trade.get('pair') != payload.pair or bool(trade.get('is_short')) != payload.is_short
                    or trade.get('amount') != payload.amount
                    or trade.get('open_timestamp') != payload.open_timestamp):
                raise HTTPException(409, 'Entry changed. Refresh and review it again; no cancel submitted')
            entries = _open_entry_orders(trade)
            if not entries:
                return {'state': 'no_open_entry'}
            # A partial or racing fill keeps a position that the receiver arms
            # later, and Freqtrade may refuse a below-minimum partial cancel
            # while still answering 200. Only an entry with no fill is cancelled here.
            if not _nothing_filled(trade):
                raise HTTPException(409, 'Part of this entry has filled, or the fill is not reported. The dashboard cancels only an entry with nothing filled; no cancel submitted')
            # Freqtrade's open-order route cancels every open order on the
            # trade, so refuse unless the only one is the confirmed entry.
            if (len(entries) != 1 or entries[0].get('order_id') != payload.order_id
                    or sum(1 for o in trade.get('orders', []) if o.get('is_open')) != 1):
                raise HTTPException(409, 'Open orders changed. Refresh and review them again; no cancel submitted')
            reserve(bot_key, trade_id, payload, 'cancel-entry', 'A cancel request already exists. Check current orders before another action')
            # A 2xx is not confirmation: report cancelled only when a fresh
            # snapshot shows the order gone and still nothing filled.
            confirmed = False
            try:
                result = await client.delete(bot['url'].rstrip('/') + f'/api/v1/trades/{trade_id}/open-order')
                if result.is_success:
                    confirmed = _entry_cancel_confirmed(await snapshot(client, bot, trade_id, 'cancel unconfirmed'), payload.order_id)
            except (httpx.HTTPError, HTTPException):
                confirmed = False
            with connect() as db:
                db.execute('UPDATE actions SET state=? WHERE request_id=?', ('accepted' if confirmed else 'unknown', str(payload.request_id)))
            return {'state': 'cancelled' if confirmed else 'unconfirmed', 'replayed': False}
