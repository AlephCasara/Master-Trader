"""An unfilled limit entry is an order, not a position (#159).

Fixtures follow Freqtrade's /status shape for a limit-in-zone entry that is
still resting: ``amount`` 0 and one open entry order with ``filled`` 0.
Values are synthetic.
"""
import asyncio
import copy

import app

PLACED_MS = 1_790_000_000_000
CFG = {"dry_run": False, "timeframe": "5m",
       "unfilledtimeout": {"entry": 1440, "exit": 43200, "unit": "minutes"}}
PENDING = {
    "trade_id": 15, "pair": "ENA/USDC:USDC", "is_open": True, "is_short": False,
    "amount": 0.0, "amount_requested": 413.0, "open_rate": 0.225, "current_rate": 0.231,
    "stake_amount": 30.98, "leverage": 3.0, "open_timestamp": PLACED_MS,
    "stop_loss_abs": 0.21975, "stop_loss_ratio": -0.07, "profit_abs": 0.0, "profit_pct": 0.0,
    "enter_tag": "signal:9001|sl:0.2", "nr_of_successful_entries": 0, "nr_of_successful_exits": 0,
    "orders": [{"order_id": "0xentry", "ft_order_side": "buy", "order_type": "limit", "status": "open",
                "is_open": True, "amount": 413.0, "filled": 0.0, "remaining": 413.0, "price": 0.225,
                "order_timestamp": PLACED_MS}],
}


def partial():
    trade = copy.deepcopy(PENDING)
    trade.update(amount=200.0, nr_of_successful_entries=0)
    trade["orders"][0].update(filled=200.0, remaining=213.0, status="open")
    return trade


def test_resting_entry_with_nothing_filled_is_pending():
    assert app._entry_state(PENDING, CFG) == {
        "state": "pending", "side": "buy", "order_id": "0xentry", "order_type": "limit",
        "price": 0.225, "requested": 413.0, "filled": 0.0, "placed_ts": PLACED_MS,
        "expires_ts": PLACED_MS + 1440 * 60_000, "cancellable": True,
    }


def test_partially_filled_entry_reports_filled_against_requested():
    state = app._entry_state(partial(), CFG)
    assert (state["state"], state["filled"], state["requested"]) == ("partial", 200.0, 413.0)


def test_hyperliquid_lagging_order_never_hides_a_filled_position():
    # Hyperliquid can report the trade amount before the order record's
    # `filled` catches up. A trade that holds quantity is never "pending".
    lagging = copy.deepcopy(PENDING)
    lagging.update(amount=413.0, nr_of_successful_entries=1)
    state = app._entry_state(lagging, CFG)
    assert state["state"] == "filled" and not state.get("cancellable")
    lagging["amount"] = 200.0
    state = app._entry_state(lagging, CFG)
    assert (state["state"], state["filled"], state["requested"], state["cancellable"]) == ("partial", 200.0, 413.0, False)


def test_only_an_entry_with_no_fill_at_all_is_cancellable():
    assert app._entry_state(PENDING, CFG)["cancellable"] is True
    assert app._entry_state(partial(), CFG)["cancellable"] is False
    # Fill amount not reported: still shown as pending, never cancellable.
    unreported = copy.deepcopy(PENDING)
    del unreported["orders"][0]["filled"]
    state = app._entry_state(unreported, CFG)
    assert (state["state"], state["cancellable"]) == ("pending", False)


def test_short_entry_uses_sell_orders_and_seconds_timeout():
    trade = copy.deepcopy(PENDING)
    trade["is_short"] = True
    trade["orders"][0]["ft_order_side"] = "sell"
    state = app._entry_state(trade, {"unfilledtimeout": {"entry": 600, "unit": "seconds"}})
    assert (state["state"], state["side"], state["expires_ts"]) == ("pending", "sell", PLACED_MS + 600_000)


def test_filled_entry_and_unknown_cases():
    filled = copy.deepcopy(PENDING)
    filled.update(amount=413.0)
    filled["orders"][0].update(status="closed", is_open=False, filled=413.0)
    assert app._entry_state(filled, CFG) == {"state": "filled", "side": "buy"}
    # An exit order resting on a filled position is not an entry.
    filled["orders"].append({"ft_order_side": "sell", "status": "open", "is_open": True, "filled": 0})
    assert app._entry_state(filled, CFG)["state"] == "filled"
    # Older responses without orders: a positive amount is a filled position.
    assert app._entry_state({"amount": 5.0}, CFG)["state"] == "filled"
    assert app._entry_state({"amount": 0.0}, CFG)["state"] == "unknown"
    # Unrecognized timeout unit: expiry stays unknown rather than guessed.
    assert app._entry_state(PENDING, {"unfilledtimeout": {"entry": 1, "unit": "hours"}})["expires_ts"] is None


def test_posted_stop_comes_from_the_receiver_entry_tag():
    assert app._tagged_stop("signal:9001|sl:0.2") == 0.2
    assert app._tagged_stop("signal:9001") is None
    assert app._tagged_stop("signal:9001|sl:bad") is None
    assert app._tagged_stop(None) is None


def test_unfilled_entry_carries_no_stop_risk_or_notional():
    filled = {"amount": 10.0, "current_rate": 2.0, "stake_amount": 20.0, "stop_loss_ratio": -0.05}
    assert app._capital_at_risk([PENDING, filled]) == {"abs_loss": 1.0, "open_count": 1, "open_notional": 20.0}
    assert app._capital_at_risk([PENDING])["abs_loss"] == 0.0


def test_poll_snapshot_marks_pending_entry(monkeypatch):
    bot = app._bot_meta("killers-ft")
    payloads = {"profit": {"bot_start_timestamp": 0}, "status": [PENDING],
                "balance": {"starting_capital": 98.0, "total_bot": 98.0, "total": 98.0},
                "show_config": CFG, "whitelist": {"whitelist": []}}

    async def fake_get(client, url, path, *args, **kwargs):
        return payloads.get(path.split("?")[0]), None

    async def fake_plain(*args, **kwargs):
        return None, "unavailable"

    async def fake_history(client, bot, epoch_start_ts_ms):
        return [], True, None

    monkeypatch.setattr(app, "_get", fake_get)
    monkeypatch.setattr(app, "_get_plain", fake_plain)
    monkeypatch.setattr(app, "_fetch_epoch_trades", fake_history)
    monkeypatch.setattr(app, "killers_tp_ladder", lambda *a, **k: {})
    snapshot = asyncio.run(app._poll_bot(None, bot))
    row = snapshot["open_trades"][0]
    assert row["entry"]["state"] == "pending"
    assert row["entry"]["expires_ts"] == PLACED_MS + 86_400_000
    assert row["posted_stop"] == 0.2
    assert snapshot["capital_at_risk"]["abs_loss"] == 0.0
