"""The one way ft_userdata scripts post a report to trade-webhook (#101).

Every script used to carry its own hardcoded copy of the URL, and seven of
them still pointed at `/webhooks/freqtrade`, the Mac claude-assistant route
from before the VPS move. trade-webhook serves `/freqtrade/event` and parses
the body as JSON, so those reports got a 404 (and form-encoded bodies a 400).

    send_status(message, bot_name="walk-forward")

posts `{"type": "status", "bot_name": ..., "status": ...}` as JSON to
`$WEBHOOK_URL`, defaulting to the host-loopback port trade-webhook publishes
on the VPS. `bot_name` names the lake file (`<bot_name>.jsonl`) and the
`[<bot_name>]` prefix of the Telegram line.

When TRADE_WEBHOOK_NOTIFY_TOKEN is set, it is sent as the X-Notify-Token
header, the shared secret trade-webhook already checks on /test/notify (#59)
and is set to check on /freqtrade/event (#97). Unset sends no header.
"""

from __future__ import annotations

import logging
import os

import requests

DEFAULT_WEBHOOK_URL = "http://localhost:8088/freqtrade/event"
NOTIFY_TOKEN_HEADER = "X-Notify-Token"

log = logging.getLogger(__name__)


def webhook_url() -> str:
    return os.environ.get("WEBHOOK_URL", "").strip() or DEFAULT_WEBHOOK_URL


def build_payload(message: str, bot_name: str) -> dict:
    return {"type": "status", "bot_name": bot_name, "status": message}


def build_headers() -> dict:
    token = os.environ.get("TRADE_WEBHOOK_NOTIFY_TOKEN", "").strip()
    return {NOTIFY_TOKEN_HEADER: token} if token else {}


def send_status(message: str, bot_name: str, timeout: float = 10) -> bool:
    """POST one status report. True on a 2xx; anything else is logged, never raised."""
    url = webhook_url()
    try:
        resp = requests.post(
            url,
            json=build_payload(message, bot_name),
            headers=build_headers(),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        log.warning("%s report not sent: webhook %s unreachable: %s", bot_name, url, exc)
        return False
    if 200 <= resp.status_code < 300:
        log.info("%s report sent (%d chars)", bot_name, len(message))
        return True
    log.warning(
        "%s report not sent: webhook %s returned HTTP %d: %s",
        bot_name, url, resp.status_code, resp.text[:200],
    )
    return False
