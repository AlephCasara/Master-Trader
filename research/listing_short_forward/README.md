# listing-short-forward-2026-07 — PAPER forward monitor

Preregistered forward test of the "short a freshly-listed Binance futures perp" thesis, run
as a **paper** monitor on the VPS (host-side python, stdlib only, no fleet containers, never
touches `/etc/dokploy`). Same operational pattern as `hl_carry_2026-07/shadow`.

## What it does

`monitor.py` is one long-running process:

1. **Poll** the Binance CMS "New Cryptocurrency Listing" announcements endpoint (catalog 48,
   page 1, `pageSize=20`, browser UA — the exact URL/headers verified working from this VPS,
   copied from `../listing_reaction_2026-07/fetch_announcements.py`) every **60s**. Seen
   article IDs are tracked in `state.json`.
2. On a **new** article whose title matches a **futures/perpetual launch** pattern (same
   classifier as `../listing_reaction_2026-07/parse_symbols.py`, futures branch), extract the
   token symbol(s). For each token, check **Hyperliquid perp availability** via `POST info
   {"type":"meta"}` (universe cached 1h in `state.json`). If available:
   - Record event start `{article_id, title, symbols, release_ts, detected_ts}` →
     `data/events.jsonl`.
   - **Entry** at `t+60s` after `release_ts` (or at `detected_ts` if detection lagged release
     by >60s — both timestamps recorded, `late_detection` flag set): snapshot HL `l2Book`,
     paper **SHORT at best BID** (crossing = we hit the bid), **taker 4.5bps**, **$1000**
     notional.
   - **Snapshots** every 5min until `t+4h`, then hourly until `t+24h`. Schedules persist in
     `state.json`, so a restart resumes all pending positions.
   - **Exits** at `t+4h` and `t+24h`: paper **buy-to-close at best ASK** (taker) →
     net short return.
3. Each snapshot also records **Binance spot `bookTicker`** for the token if it is spot-listed
   (secondary data; `null` if not on spot).
4. Loud `[listing-monitor]` log lines to `monitor.log` + stdout (journal). Daily file rotation
   (gzip yesterday's `ticks_YYYYMMDD.jsonl`, prune >120d). Every failure is **skip-and-log**;
   the loop never crashes.

### Frozen config (prereg — do not deviate)
- entry: best BID, taker fee `0.00045`, notional `$1000`, side SHORT
- exit: best ASK, taker fee `0.00045`, at `t+4h` and `t+24h`
- net short return = `(1 - fee) - (exit_ask / entry_bid) * (1 + fee)`; `pnl_usd = net * 1000`
- venue: Hyperliquid perp only (availability gate); Binance spot BBO is secondary/context only

## Files
- `monitor.py` — the service
- `state.json` — seen IDs, pending positions + their persisted schedules, HL meta cache
- `data/events.jsonl` — one line per detected listing event
- `data/ticks_YYYYMMDD.jsonl` — entry / snapshot / exit rows (gzipped after the day rolls)
- `monitor.log` — human log

## Simulated events — EXCLUDE from analysis
Real listing events are ~weekly, so the pipeline is proven with `--simulate`, which injects a
fake announcement for an existing HL coin (BTC) and runs entry -> snapshots -> exits on a
compressed clock against **real** HL prices. Every simulated row carries **`"simulated": true`**
in both `data/events.jsonl` and `data/ticks_*.jsonl`, and article IDs are prefixed `SIM-`.

**Exclude pattern for any analysis: drop every row where `simulated == true` (equivalently,
`article_id` starts with `SIM-`).** Only `simulated == false` rows are prereg data.

## Prereg unblind
The prereg stays **blinded** until **>= 8 real (non-simulated) completed events** (an event is
"completed" once its `t+24h` exit row exists). Before that: collect only, do not compute or act
on aggregate net-return stats. At >= 8 events, unblind and evaluate net short return at the
`h4` and `h24` horizons.

## Ops
- Service: systemd **user** unit `listing-monitor.service` (`Restart=always`), running as
  `ubuntu`.
- Status:  `systemctl --user status listing-monitor`
- Logs:    `journalctl --user -u listing-monitor -f`  (or `tail -f monitor.log`)
- **Stop:** `systemctl --user stop listing-monitor`   (disable persistence:
  `systemctl --user disable listing-monitor`)
- Restart: `systemctl --user restart listing-monitor`  (pending schedules resume from state.json)
- Run the pipeline proof by hand:
  `python3 monitor.py --simulate`
