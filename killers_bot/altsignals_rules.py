"""Deterministic parser for the AltSignals VIP channel (trial lane).

The channel posts a fixed template (signal post, fill confirmation, TP/SL
notices, cancel/close notices), so no LLM is involved: `classify_altsignals`
is the observer's fast path for this lane and ALWAYS returns a classification
dict in the schema `classifier.classify()` produces. Anything it cannot read
is a `chat`, never None, so nothing falls through to a model.

Safety nets here are only the channel-specific ones (typo guard, repost dedup,
optional Hyperliquid universe check). The receiver keeps its own risk gates.
"""
import json
import logging
import math
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DUP_WINDOW_SEC = 900
MAX_SL_DISTANCE = 0.25
HL_CACHE_SEC = 300
HL_RETRY_SEC = 30
HL_TIMEOUT_SEC = 3
HL_INFO_URL_ENV = "TRIAL_ALTSIGNALS_HL_INFO_URL"

_NUM = r"(\d+(?:\.\d+)?)"
_HDR_RE = re.compile(
    r"^\s*(?:Coin\s*:+\s*)?[$#]?([A-Z0-9]{2,15}?)(?:/?USDT)?\s+(long|short)\b",
    re.IGNORECASE | re.MULTILINE,
)
_ENTRY_RE = re.compile(r"Entry\s*:+\s*" + _NUM, re.IGNORECASE)
_SL_RE = re.compile(r"(?:SL|Stop\s*-?\s*loss)\s*:+\s*" + _NUM, re.IGNORECASE)
_TARGET_RE = re.compile(r"Target\s*(\d+)\s*:+\s*" + _NUM, re.IGNORECASE)
_LEVERAGE_RE = re.compile(
    r"Leverage\s*:+\s*(?:(isolated|cross)\s*)?x?\s*(\d+)\s*x?", re.IGNORECASE)
_REF_RE = re.compile(r"#([A-Z0-9]{1,15})/USDT")
_PCT_RE = re.compile(r"(Profit|Loss)\s*:\s*" + _NUM + r"\s*%", re.IGNORECASE)
_AVG_ENTRY_RE = re.compile(r"Average Entry Price\s*:\s*" + _NUM, re.IGNORECASE)
_TP_HIT_RE = re.compile(r"take-profit target\s*(\d+)", re.IGNORECASE)

# Phrase -> note for the close notices that end a position.
_CLOSE_EVENTS = (
    ("stop target hit", "stop_hit"),
    ("all targets achieved", "all_targets"),
    ("manually cancelled", "manual_cancel"),
    ("closed due to opposite direction", "opposite_signal"),
    ("closed at trailing stoploss", "trailing_stop"),
)


def _classification(msg_id: int, kind: str, notes: str, *, symbol=None,
                    direction=None, entry=None, entry_range=None, sl=None,
                    pct=None, targets=None, confidence=1.0) -> dict:
    out = {
        "id": msg_id,
        "kind": kind,
        "signal_id": None,
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "entry_range": entry_range,
        "sl": sl,
        "tp": None,
        "pct": pct,
        "applies_to": None,
        "confidence": confidence,
        "notes": notes,
    }
    if targets:
        out["targets"] = targets
    return out


def _chat(msg_id: int, notes: str, symbol=None, confidence=1.0) -> dict:
    return _classification(msg_id, "chat", notes, symbol=symbol,
                           confidence=confidence)


def _base_symbol(raw: str) -> str:
    s = raw.upper().lstrip("$#")
    return re.sub(r"/?USDT$", "", s)


def _signed_pct(text: str) -> Optional[float]:
    m = _PCT_RE.search(text)
    if not m:
        return None
    value = float(m.group(2))
    return -value if m.group(1).lower() == "loss" else value


def _to_datetime(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _find_duplicate(conn, msg_id: int, symbol: str, direction: str,
                    entry: float, sl: float, when) -> Optional[int]:
    """Earlier `open` of this lane with the same symbol/direction/entry/SL
    inside the dedup window. The message's own id is never a candidate, so an
    edit of the original post is not flagged against itself."""
    now = _to_datetime(when) or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT c.msg_id, c.entry_lo, c.sl, "
        "       COALESCE(r.posted_at, c.classified_at) "
        "FROM classifications c LEFT JOIN raw_messages r ON r.msg_id = c.msg_id "
        "WHERE c.kind = 'open' AND c.symbol = ? AND c.direction = ? "
        "AND c.msg_id < ? ORDER BY c.msg_id DESC LIMIT 50",
        (symbol, direction, msg_id),
    ).fetchall()
    for prev_id, prev_entry, prev_sl, prev_at in rows:
        if prev_entry is None or prev_sl is None:
            continue
        if not (math.isclose(prev_entry, entry, rel_tol=1e-9)
                and math.isclose(prev_sl, sl, rel_tol=1e-9)):
            continue
        prev_dt = _to_datetime(prev_at)
        if prev_dt is None or abs((now - prev_dt).total_seconds()) <= DUP_WINDOW_SEC:
            return prev_id
    return None


# ── Hyperliquid perp universe (optional) ───────────────────────────────────

_universe_cache: dict = {"url": None, "at": 0.0, "names": None, "failed_at": 0.0}


def _fetch_universe(url: str) -> set:
    req = urllib.request.Request(
        url, data=json.dumps({"type": "meta"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=HL_TIMEOUT_SEC) as resp:
        body = json.loads(resp.read())
    return {u["name"] for u in body["universe"]}


def _on_hyperliquid(symbol: str) -> bool:
    """False only when the universe was read and the coin is absent. Any
    error answers True (fail open): the receiver is the fail-closed backstop."""
    url = os.getenv(HL_INFO_URL_ENV, "").strip()
    if not url:
        return True
    cache = _universe_cache
    now = time.monotonic()
    if cache["url"] != url:
        cache.update(url=url, at=0.0, names=None, failed_at=0.0)
    fresh = cache["names"] is not None and now - cache["at"] < HL_CACHE_SEC
    if not fresh:
        if cache["failed_at"] and now - cache["failed_at"] < HL_RETRY_SEC:
            return True
        try:
            if urlparse(url).scheme not in ("http", "https"):
                raise ValueError("unsupported scheme")
            cache.update(names=_fetch_universe(url), at=now, failed_at=0.0)
        except Exception as e:
            cache["failed_at"] = now
            logger.warning("[ALTSIGNALS] HL universe fetch failed (%s) — "
                           "failing open", e)
            return True
    names = cache["names"]
    return symbol in names or f"k{symbol}" in names


# ── Entry point ────────────────────────────────────────────────────────────


def _parse_open(text: str, msg_id: int, conn, date) -> Optional[dict]:
    hdr = _HDR_RE.search(text)
    entry_m = _ENTRY_RE.search(text)
    sl_m = _SL_RE.search(text)
    targets = [float(v) for _, v in _TARGET_RE.findall(text)]
    if not (hdr and entry_m and (sl_m or targets)):
        return None

    symbol = _base_symbol(hdr.group(1))
    direction = hdr.group(2).lower()
    entry = float(entry_m.group(1))
    sl = float(sl_m.group(1)) if sl_m else None
    lev_m = _LEVERAGE_RE.search(text)
    lev = f" lev=x{lev_m.group(2)}" if lev_m else ""

    def reject(why: str) -> dict:
        return _classification(
            msg_id, "chat", f"typo_guard: {why}", symbol=symbol,
            direction=direction, entry=entry, sl=sl, targets=targets)

    if entry <= 0:
        return reject("entry not positive")
    if sl is None or sl <= 0:
        return reject("no SL")
    if abs(entry - sl) / entry > MAX_SL_DISTANCE:
        return reject(f"SL {sl} is more than {MAX_SL_DISTANCE:.0%} from entry {entry}")
    if (direction == "long" and sl >= entry) or (direction == "short" and sl <= entry):
        return reject(f"SL {sl} on the wrong side of entry {entry}")
    if any((direction == "long" and t <= entry) or (direction == "short" and t >= entry)
           for t in targets):
        return reject(f"targets {targets} on the wrong side of entry {entry}")

    if conn is not None:
        dup = _find_duplicate(conn, msg_id, symbol, direction, entry, sl, date)
        if dup is not None:
            return _classification(
                msg_id, "chat", f"duplicate_of={dup}", symbol=symbol,
                direction=direction, entry=entry, sl=sl)

    if not _on_hyperliquid(symbol):
        return _classification(
            msg_id, "chat", "not_on_hyperliquid", symbol=symbol,
            direction=direction, entry=entry, sl=sl)

    return _classification(
        msg_id, "open",
        f"altsignals open{lev} targets={targets}",
        symbol=symbol, direction=direction, entry=entry,
        entry_range=[entry, entry], sl=sl, targets=targets)


def _parse_notice(text: str, msg_id: int) -> Optional[dict]:
    ref = _REF_RE.search(text)
    if not ref:
        return None
    symbol = ref.group(1)
    lower = text.lower()
    if "all entries achieved" in lower:
        avg = _AVG_ENTRY_RE.search(text)
        note = "fill_confirm" + (f" avg_entry={avg.group(1)}" if avg else "")
        return _chat(msg_id, note, symbol)
    for phrase, event in _CLOSE_EVENTS:
        if phrase in lower:
            return _classification(
                msg_id, "close_full", event, symbol=symbol,
                pct=_signed_pct(text))
    tp = _TP_HIT_RE.search(text)
    if tp:
        return _chat(msg_id, f"tp_hit target={tp.group(1)}", symbol)
    return _chat(msg_id, "unparsed notice", symbol, confidence=0.0)


def classify_altsignals(text: Optional[str], msg_id: int, conn=None,
                        date=None) -> dict:
    """Classify one AltSignals message. Never returns None.

    `conn` is this lane's observer DB (used for the repost dedup; skipped when
    None) and `date` the message timestamp the window is measured from.
    """
    t = (text or "").strip() if isinstance(text, str) else ""
    if not t:
        return _chat(msg_id, "empty message")
    opened = _parse_open(t, msg_id, conn, date)
    if opened is not None:
        return opened
    notice = _parse_notice(t, msg_id)
    if notice is not None:
        return notice
    if re.fullmatch(r"close", t, re.IGNORECASE):
        return _chat(msg_id, "bare close without symbol")
    return _chat(msg_id, "unparsed", confidence=0.0)
