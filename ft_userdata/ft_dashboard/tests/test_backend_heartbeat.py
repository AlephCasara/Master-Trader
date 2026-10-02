"""Classifier backend heartbeat probe (bd Master-Trader-obt).

A home-box outage must be visible as recorded history, not inferred from
missing decisions later. These tests lock the probe contract: what is sent,
how failures are classified, how history persists, and what
/api/backend_health and /api/state expose.
"""
import io
import json
import socket
import time
import urllib.error

from fastapi.testclient import TestClient

import app


def _configure(monkeypatch, tmp_path):
    monkeypatch.setenv("HEARTBEAT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(app, "CLASSIFIER_HEARTBEAT_ENDPOINT", "http://classify.test/v1")
    monkeypatch.setattr(app, "CLASSIFIER_HEARTBEAT_BEARER", "secret-token")
    monkeypatch.setattr(app, "CLASSIFIER_HEARTBEAT_MODEL", "qwen3-4b")
    monkeypatch.setattr(app, "CLASSIFIER_HEARTBEAT_INTERVAL_SEC", 900)
    monkeypatch.setattr(app, "_heartbeat_probes", [])


def test_ok_probe_records_and_persists_atomically(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    seen = {}

    def fake_post(url, body, headers):
        seen["url"], seen["body"], seen["headers"] = url, json.loads(body), headers
        return 200

    monkeypatch.setattr(app, "_heartbeat_http_post", fake_post)
    probe = app._run_heartbeat_probe()

    assert probe["ok"] is True
    assert probe["error_class"] == "ok"
    assert isinstance(probe["latency_ms"], int)
    assert seen["url"] == "http://classify.test/v1/chat/completions"
    # Full-URL convention (MT_CLASSIFY_ENDPOINT style) must not double the path.
    assert app._probe_url("http://classify.test/v1/chat/completions") == \
        "http://classify.test/v1/chat/completions"
    assert seen["body"] == {
        "model": "qwen3-4b",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    # Bearer always sent when configured — the backend proxy desyncs
    # connections that auth-fail with a body.
    assert seen["headers"]["Authorization"] == "Bearer secret-token"

    data = json.loads((tmp_path / "backend_heartbeat.json").read_text())
    assert data["probes"][-1]["ok"] is True
    assert not list(tmp_path.glob("*.tmp")), "atomic write must leave no tmp file"


def test_dns_failure_is_classified(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    def fake_post(url, body, headers):
        raise urllib.error.URLError(
            socket.gaierror(-2, "Name or service not known")
        )

    monkeypatch.setattr(app, "_heartbeat_http_post", fake_post)
    probe = app._run_heartbeat_probe()
    assert probe["ok"] is False
    assert probe["error_class"] == "dns_fail"


def test_http_and_timeout_failures_are_classified(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    def http_error(url, body, headers):
        raise urllib.error.HTTPError(
            url, 502, "Bad Gateway", None, io.BytesIO(b"")
        )

    monkeypatch.setattr(app, "_heartbeat_http_post", http_error)
    assert app._run_heartbeat_probe()["error_class"] == "http_error"

    def timeout(url, body, headers):
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr(app, "_heartbeat_http_post", timeout)
    assert app._run_heartbeat_probe()["error_class"] == "timeout"


def test_unreadable_state_file_starts_empty(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    (tmp_path / "backend_heartbeat.json").write_text("{not json")
    app._load_heartbeat()
    assert app._heartbeat_probes == []


def test_backend_health_shape_and_24h_failure_count(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    now = time.time()
    monkeypatch.setattr(app, "_heartbeat_probes", [
        {"ts": now - 90000, "ok": False, "latency_ms": 5, "error_class": "dns_fail"},
        {"ts": now - 100, "ok": False, "latency_ms": 12, "error_class": "dns_fail"},
        {"ts": now - 50, "ok": True, "latency_ms": 200, "error_class": "ok"},
        {"ts": now - 10, "ok": False, "latency_ms": None, "error_class": "dns_fail"},
    ])
    client = TestClient(app.app)

    body = client.get("/api/backend_health").json()
    assert body["interval_sec"] == 900
    assert body["endpoint"] == "classify.test"  # host only, never the URL
    assert body["last"]["error_class"] == "dns_fail"
    assert body["failures_24h"] == 2
    assert [p["ok"] for p in body["history_24h"]] == [False, True, False]

    state = client.get("/api/state").json()
    assert state["classifier_backend"]["failures_24h"] == 2
    assert state["classifier_backend"]["last"]["ok"] is False
    assert len(state["classifier_backend"]["history_24h"]) == 3


def test_empty_history_reports_null_last_and_zero_failures(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "_heartbeat_probes", [])
    body = TestClient(app.app).get("/api/backend_health").json()
    assert body["last"] is None
    assert body["failures_24h"] == 0
    assert body["history_24h"] == []
