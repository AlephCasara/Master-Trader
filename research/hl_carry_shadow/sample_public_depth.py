#!/usr/bin/env python3
"""Forward sample of public order-book depth for the carry legs (issue #27).

The shadow archive records only HL levels 1-2 and Binance level 1, so it cannot
price a $1,000 leg that is larger than the touch. This samples deeper public
books (HL l2Book, up to 20 levels; Binance spot /api/v3/depth, 100 levels) and
measures what each modeled leg would cost to take at the sampled moment:

  HL short exit  = buy  on HL      (walk asks)
  BN spot entry  = buy  on Binance (walk asks)
  BN spot exit   = sell on Binance (walk bids)
  HL short entry = maker; reported as queue ahead (displayed ask size) only

Read-only public market data, no keys. Run it from a host that does NOT share
the fleet's Hyperliquid IP budget (see README "Request budget"). Output is
one JSON line per (sample, coin) plus a summary when --summarize is given.

    python3 sample_public_depth.py OUT.jsonl --coins ZRO,KAITO --samples 12 --every 300
    python3 sample_public_depth.py OUT.jsonl --summarize > table.json
"""
import argparse
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ft_userdata.analysis.costs import book_impact_bps  # noqa: E402

HL_INFO = "https://api.hyperliquid.xyz/info"
BN_DEPTH = "https://api.binance.com/api/v3/depth"
NOTIONALS = (1000.0, 3000.0)


def _get(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "User-Agent": "mt-research/1"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def legs(coin):
    hl = _get(HL_INFO, {"type": "l2Book", "coin": coin})
    hl_bids = [(float(x["px"]), float(x["sz"])) for x in hl["levels"][0]]
    hl_asks = [(float(x["px"]), float(x["sz"])) for x in hl["levels"][1]]
    bn = _get(BN_DEPTH + "?" + urllib.parse.urlencode({"symbol": coin + "USDT", "limit": 100}))
    bn_bids = [(float(p), float(q)) for p, q in bn["bids"]]
    bn_asks = [(float(p), float(q)) for p, q in bn["asks"]]
    hl_mid = (hl_bids[0][0] + hl_asks[0][0]) / 2
    bn_mid = (bn_bids[0][0] + bn_asks[0][0]) / 2
    row = {"coin": coin, "hl_time_ms": hl.get("time"),
           "hl_spread_bps": (hl_asks[0][0] - hl_bids[0][0]) / hl_mid * 1e4,
           "bn_spread_bps": (bn_asks[0][0] - bn_bids[0][0]) / bn_mid * 1e4,
           "hl_ask_l1_usd": hl_asks[0][0] * hl_asks[0][1],
           "bn_ask_l1_usd": bn_asks[0][0] * bn_asks[0][1],
           "bn_bid_l1_usd": bn_bids[0][0] * bn_bids[0][1],
           "hl_levels": len(hl_asks), "bn_levels": len(bn_asks)}
    for n in NOTIONALS:
        k = int(n)
        # Beyond-touch impact is what the shadow model does not charge: it
        # already prices every taker leg at the best quote.
        row[f"hl_buy_{k}_beyond_touch_bps"] = book_impact_bps(hl_asks, n, "buy")
        row[f"bn_buy_{k}_beyond_touch_bps"] = book_impact_bps(bn_asks, n, "buy")
        row[f"bn_sell_{k}_beyond_touch_bps"] = book_impact_bps(bn_bids, n, "sell")
    return row


def sample(out, coins, samples, every):
    with open(out, "a") as f:
        for i in range(samples):
            started = time.time()
            for coin in coins:
                try:
                    row = legs(coin)
                except Exception as exc:  # public endpoint hiccup: record, continue
                    row = {"coin": coin, "error": repr(exc)}
                row["sampled_at"] = time.time()
                f.write(json.dumps(row) + "\n")
                time.sleep(0.5)
            f.flush()
            if i < samples - 1:
                time.sleep(max(0.0, every - (time.time() - started)))


def summarize(out):
    rows = [json.loads(line) for line in Path(out).read_text().splitlines() if line.strip()]
    rows = [r for r in rows if "error" not in r]
    by_coin = {}
    for r in rows:
        by_coin.setdefault(r["coin"], []).append(r)
    keys = [k for k in rows[0] if k.endswith("_bps") or k.endswith("_usd")] if rows else []
    table = {}
    for coin, rs in sorted(by_coin.items()):
        entry = {"samples": len(rs)}
        for k in keys:
            vals = [r[k] for r in rs if r.get(k) is not None]
            entry[k] = {"median": statistics.median(vals), "max": max(vals),
                        "n_unfillable": len(rs) - len(vals)} if vals else None
        table[coin] = entry
    span = (min(r["sampled_at"] for r in rows), max(r["sampled_at"] for r in rows)) if rows else None
    return {"sampled_from": span, "notionals": NOTIONALS, "coins": table,
            "note": "Forward sample of current public books, not the books at the "
                    "archived decision times. Beyond-touch = extra cost over the "
                    "best quote, which the shadow model already charges."}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--coins", default="ZRO,KAITO,ZEC,ASTER,EIGEN,GRAM,PUMP,TAO,ONDO,UNI,ENA")
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--every", type=float, default=300.0)
    ap.add_argument("--summarize", action="store_true")
    a = ap.parse_args()
    if a.summarize:
        print(json.dumps(summarize(a.out), indent=2))
    else:
        sample(a.out, [c.strip() for c in a.coins.split(",") if c.strip()], a.samples, a.every)
