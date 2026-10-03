"""Listing-short monitor (#127): an erroring exit is retried, not skipped forever."""
import importlib.util
import urllib.error
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "listing_monitor",
    Path(__file__).resolve().parents[1] / "research" / "listing_short_forward" / "monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(monitor, "log", lambda msg: None)


def _pos():
    return {"position_id": "P00001", "symbol": "TEST", "hl_name": "TEST", "release_ts": 0,
            "entry_done": True, "entry_price": 1.0, "simulated": False,
            "schedule": [{"due_ts": 1000, "kind": "exit", "horizon": "h24"}]}


def _rate_limited(*a, **k):
    raise urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)


def test_rate_limited_exit_stays_pending_inside_the_window(monkeypatch):
    monkeypatch.setattr(monitor, "do_exit", _rate_limited)
    state = {"pending": [_pos()]}
    monitor.process_pending(state, 1000 + monitor.EXIT_RETRY_WINDOW)
    assert state["pending"][0]["schedule"][0]["horizon"] == "h24"


def test_exit_is_skipped_after_the_window(monkeypatch):
    monkeypatch.setattr(monitor, "do_exit", _rate_limited)
    state = {"pending": [_pos()]}
    monitor.process_pending(state, 1000 + monitor.EXIT_RETRY_WINDOW + 1)
    assert state["pending"] == []


def test_retried_exit_records_its_due_time(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, "do_exit", lambda pos, horizon, due_ts=None: calls.append(due_ts))
    state = {"pending": [_pos()]}
    monitor.process_pending(state, 1300)
    assert calls == [1000] and state["pending"] == []


def test_erroring_snapshot_is_still_skipped(monkeypatch):
    monkeypatch.setattr(monitor, "do_snapshot", _rate_limited)
    pos = _pos()
    pos["schedule"] = [{"due_ts": 1000, "kind": "snapshot", "horizon": "1h"}]
    state = {"pending": [pos]}
    monitor.process_pending(state, 1000)
    assert state["pending"] == []
