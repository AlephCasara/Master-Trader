import base64
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location('trade_controls', Path(__file__).parents[1] / 'trade_controls.py')
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('DASHBOARD_ACTION_DB', str(tmp_path / 'actions.sqlite'))
    app = FastAPI()
    m.install(app, [{'key':'test','url':'http://executor'}], lambda _: ('operator','synthetic-secret'))
    client = TestClient(app, base_url='https://dashboard.test')
    headers = {'Origin':'https://dashboard.test', 'X-Trade-Action':'close', 'Authorization':'Basic '+base64.b64encode(b'operator:synthetic-secret').decode()}
    body = {'request_id':str(uuid4()),'pair':'TEST/USDC:USDC','amount':20.,'open_timestamp':12345678.,'is_short':False}
    trade = {'trade_id':4, **{k:v for k,v in body.items() if k!='request_id'}}
    upstream = AsyncMock()
    upstream.__aenter__.return_value = upstream
    upstream.get.return_value = httpx.Response(200, json=[trade], request=httpx.Request('GET','http://executor'))
    upstream.post.return_value = httpx.Response(200, json={'result':'created'}, request=httpx.Request('POST','http://executor'))
    monkeypatch.setattr(m.httpx, 'AsyncClient', lambda **_:upstream)
    return client, headers, body, trade, upstream


def test_confirmed_close_and_replay_submit_once(setup):
    client,h,b,t,up = setup
    r=client.post('/api/trades/test/4/close',headers=h,json=b)
    assert r.json()['state']=='accepted'
    assert client.post('/api/trades/test/4/close',headers=h,json=b).json()['replayed']
    up.post.assert_awaited_once_with('http://executor/api/v1/forceexit',json={'tradeid':'4','ordertype':'market'})
    b['request_id']=str(uuid4())
    assert client.post('/api/trades/test/4/close',headers=h,json=b).status_code==409
    assert up.post.await_count==1


@pytest.mark.parametrize('header,value,status',[('Origin','https://hostile.test',403),('X-Trade-Action','',403),('Authorization','Basic wrong',401),('Sec-Fetch-Site','cross-site',403)])
def test_auth_and_origin_fail_before_upstream(setup,header,value,status):
    client,h,b,t,up=setup; h[header]=value
    assert client.post('/api/trades/test/4/close',headers=h,json=b).status_code==status
    up.get.assert_not_awaited(); up.post.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('amount',19),('pair','OTHER/USDC:USDC'),('open_timestamp',23456789),('is_short',True)])
def test_stale_confirmation_cannot_close_changed_position(setup,field,value):
    client,h,b,t,up=setup; b[field]=value
    assert client.post('/api/trades/test/4/close',headers=h,json=b).status_code==409
    up.post.assert_not_awaited()


def test_unknown_submission_is_not_retried(setup):
    client,h,b,t,up=setup
    up.post.side_effect=httpx.ReadTimeout('ambiguous')
    assert client.post('/api/trades/test/4/close',headers=h,json=b).json()['state']=='unknown'
    assert client.post('/api/trades/test/4/close',headers=h,json=b).json()['state']=='unknown'
    assert up.post.await_count==1


def test_closed_position_does_not_submit(setup):
    client,h,b,t,up=setup
    up.get.return_value=httpx.Response(200,json=[],request=httpx.Request('GET','http://executor'))
    assert client.post('/api/trades/test/4/close',headers=h,json=b).json()['state']=='already_closed'
    up.post.assert_not_awaited()


def test_read_failure_does_not_submit(setup):
    client,h,b,t,up=setup; up.get.side_effect=httpx.ReadTimeout('offline')
    assert client.post('/api/trades/test/4/close',headers=h,json=b).status_code==503
    up.post.assert_not_awaited()


def test_open_entry_blocks_close(setup):
    client,h,b,t,up=setup
    t['orders']=[{'is_open':True,'ft_order_side':'buy'}]
    up.get.return_value=httpx.Response(200,json=[t],request=httpx.Request('GET','http://executor'))
    assert client.post('/api/trades/test/4/close',headers=h,json=b).status_code==409
    up.post.assert_not_awaited()


def test_upstream_error_does_not_enable_blind_retry(setup):
    client,h,b,t,up=setup
    up.post.return_value=httpx.Response(502,json={'error':'upstream'},request=httpx.Request('POST','http://executor'))
    assert client.post('/api/trades/test/4/close',headers=h,json=b).json()['state']=='unknown'
    b['request_id']=str(uuid4())
    assert client.post('/api/trades/test/4/close',headers=h,json=b).status_code==409
    assert up.post.await_count==1




# ── Cancel a resting entry order (#159) ──────────────────────────────────────
# Allowed only before any fill, and reported as done only after a fresh
# /status confirms the order is gone and nothing filled.
def _status(*trades):
    return httpx.Response(200, json=list(trades), request=httpx.Request('GET', 'http://executor'))


@pytest.fixture
def pending(setup):
    client, h, b, t, up = setup
    h['X-Trade-Action'] = 'cancel-entry'
    b = {'request_id': str(uuid4()), 'pair': 'TEST/USDC:USDC', 'order_id': 'entry-1', 'amount': 0.,
         'open_timestamp': 12345678., 'is_short': False}
    t = {'trade_id': 4, 'pair': b['pair'], 'amount': 0., 'open_timestamp': b['open_timestamp'], 'is_short': False,
         'orders': [{'order_id': 'entry-1', 'is_open': True, 'ft_order_side': 'buy', 'status': 'open', 'filled': 0}]}
    # Before the DELETE the entry rests; afterwards Freqtrade has removed the trade.
    up.get.side_effect = [_status(t), _status()]
    up.delete.return_value = httpx.Response(200, json={}, request=httpx.Request('DELETE', 'http://executor'))
    return client, h, b, t, up


def test_confirmed_cancel_entry_submits_once(pending):
    client, h, b, t, up = pending
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).json()['state'] == 'cancelled'
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).json() == {'state': 'accepted', 'replayed': True}
    up.delete.assert_awaited_once_with('http://executor/api/v1/trades/4/open-order')
    b['request_id'] = str(uuid4())
    up.get.side_effect = [_status(t)]
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).status_code == 409
    assert up.delete.await_count == 1
    up.post.assert_not_awaited()


def test_cancel_confirmed_when_order_shows_cancelled_with_no_fill(pending):
    client, h, b, t, up = pending
    after = {**t, 'orders': [{**t['orders'][0], 'is_open': False, 'status': 'canceled'}]}
    up.get.side_effect = [_status(t), _status(after)]
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).json()['state'] == 'cancelled'


def test_cancel_entry_needs_its_own_action_header(pending):
    client, h, b, t, up = pending
    h['X-Trade-Action'] = 'close'
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).status_code == 403
    up.get.assert_not_awaited(); up.delete.assert_not_awaited()


@pytest.mark.parametrize('field,value', [('order_id', 'entry-2'), ('pair', 'OTHER/USDC:USDC'),
                                         ('open_timestamp', 23456789.), ('is_short', True)])
def test_stale_confirmation_cannot_cancel_changed_entry(pending, field, value):
    client, h, b, t, up = pending; b[field] = value
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).status_code == 409
    up.delete.assert_not_awaited()


def test_cancel_request_must_confirm_zero_filled(pending):
    client, h, b, t, up = pending; b['amount'] = 5.
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).status_code == 422
    up.get.assert_not_awaited(); up.delete.assert_not_awaited()


@pytest.mark.parametrize('trade_amount,order_filled', [(0., 5.), (5., 0.), (0., None)])
def test_any_fill_or_unreported_fill_refuses_cancel(pending, trade_amount, order_filled):
    # Covers a Hyperliquid order record lagging either way, and a missing fill.
    client, h, b, t, up = pending
    t['amount'] = trade_amount
    t['orders'][0]['filled'] = order_filled
    up.get.side_effect = [_status(t)]
    b['amount'] = 0.
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).status_code == 409
    up.delete.assert_not_awaited()


def test_cancel_refused_when_another_order_would_be_cancelled(pending):
    client, h, b, t, up = pending
    t['orders'].append({'order_id': 'exit-1', 'is_open': True, 'ft_order_side': 'sell', 'filled': 0})
    up.get.side_effect = [_status(t)]
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).status_code == 409
    up.delete.assert_not_awaited()


def test_filled_entry_is_not_cancelled(pending):
    client, h, b, t, up = pending
    t['orders'][0].update(is_open=False, status='closed')
    up.get.side_effect = [_status(t)]
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).json()['state'] == 'no_open_entry'
    up.delete.assert_not_awaited()


@pytest.mark.parametrize('after', [
    'order_still_open',      # HTTP 200 but Freqtrade refused or has not cancelled
    'fill_appeared',         # a fill raced the cancel; the filled part stays open
    'order_filled',          # the order record now shows a fill
    'refetch_failed',        # cannot confirm
    'delete_error',          # non-2xx from Freqtrade
])
def test_unconfirmed_cancel_is_never_reported_as_success(pending, after):
    client, h, b, t, up = pending
    if after == 'order_still_open':
        up.get.side_effect = [_status(t), _status(t)]
    elif after == 'fill_appeared':
        up.get.side_effect = [_status(t), _status({**t, 'amount': 3., 'orders': [{**t['orders'][0], 'is_open': False, 'status': 'canceled', 'filled': 3.}]})]
    elif after == 'order_filled':
        up.get.side_effect = [_status(t), _status({**t, 'orders': [{**t['orders'][0], 'is_open': False, 'status': 'canceled', 'filled': 2.}]})]
    elif after == 'refetch_failed':
        up.get.side_effect = [_status(t), httpx.ReadTimeout('offline')]
    else:
        up.delete.return_value = httpx.Response(502, json={}, request=httpx.Request('DELETE', 'http://executor'))
        up.get.side_effect = [_status(t), _status()]
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).json()['state'] == 'unconfirmed'
    # Recorded as uncertain: replay reports it, and a fresh request is refused.
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).json() == {'state': 'unknown', 'replayed': True}
    b['request_id'] = str(uuid4())
    up.get.side_effect = [_status(t)]
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).status_code == 409
    assert up.delete.await_count == 1


def test_pending_cancel_does_not_block_a_later_close(pending):
    client, h, b, t, up = pending
    filled = {'trade_id': 4, 'pair': b['pair'], 'amount': 20., 'open_timestamp': b['open_timestamp'], 'is_short': False}
    up.get.side_effect = [_status(t), _status(), _status(filled)]
    assert client.post('/api/trades/test/4/cancel-entry', headers=h, json=b).json()['state'] == 'cancelled'
    h['X-Trade-Action'] = 'close'
    close = {'request_id': str(uuid4()), 'pair': b['pair'], 'amount': 20., 'open_timestamp': b['open_timestamp'], 'is_short': False}
    assert client.post('/api/trades/test/4/close', headers=h, json=close).json()['state'] == 'accepted'


def test_existing_action_table_gains_kind_column(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / 'legacy.sqlite'
    legacy = sqlite3.connect(path)
    legacy.execute('CREATE TABLE actions (request_id TEXT PRIMARY KEY, bot TEXT, trade INTEGER, payload TEXT, opened REAL, state TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)')
    legacy.execute("INSERT INTO actions(request_id,bot,trade,payload,opened,state) VALUES('r','test',4,'{}',1,'accepted')")
    legacy.commit(); legacy.close()
    monkeypatch.setenv('DASHBOARD_ACTION_DB', str(path))
    with m.connect() as db:
        assert db.execute('SELECT kind FROM actions').fetchall() == [('close',)]
