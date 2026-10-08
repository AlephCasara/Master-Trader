# Decision: AltSignals signal lane (2026-10-08)

## Context

The AltSignals channel posts a fixed, machine-generated template: a signal post
(coin, direction, leverage, entry, targets, stop loss) and a small set of
follow-up notices (fill confirmation, take-profit hit, stop hit, all targets
achieved, manual cancel, close on an opposite signal, close at a trailing stop).

A review of about six days of captured messages found:

- the template is almost entirely parseable by rules;
- roughly half of the signal posts are identical reposts of an earlier post;
- the existing LLM prompt read the "All entries achieved" fill confirmation as a
  new open, which would create phantom or doubled positions.

The channel trades Binance USDT-M perpetuals, and several of its coins are not
listed on Hyperliquid. The bot therefore runs on Binance USDT-M futures
(dry-run) for all coins.

## Decision

- A deterministic parser (`killers_bot/altsignals_rules.py`) is the only
  classifier for this lane. No LLM is involved, and anything it cannot read is
  a `chat`, never an error.
- Forwarding for trial channels is opt-in through `TRIAL_<NAME>_RECEIVER_URL`
  and `TRIAL_<NAME>_RECEIVER_TOKEN`. Capture-only stays the default for every
  trial channel.
- One dry-run copy bot (the `KillersScalpV1` strategy on the Binance futures
  config) sits behind its own receiver instance.

## Event mapping

| Channel message | Classification |
|---|---|
| Signal post | `open` (symbol is the base coin) |
| Same symbol, direction, entry and stop as an `open` within 900 s | `chat` (`duplicate_of=<msg_id>`) |
| Missing stop, stop more than 25% from entry, stop or targets on the wrong side of entry | `chat` (`typo_guard: ...`) |
| Stop Target Hit | `close_full`, negative `pct` |
| All targets achieved | `close_full`, positive `pct` |
| Manually Cancelled | `close_full` |
| Closed due to opposite direction signal | `close_full` |
| Closed at trailing stoploss | `close_full` |
| All entries achieved (fill confirmation) | `chat` |
| Take-Profit target N | `chat` |
| Bare `close`, anything else | `chat` |

The receiver builds its take-profit ladder only from a `TARGETS:` text line, so
for an `open` the observer appends `TARGETS: t1 - t2 ...` to the copy of the
message it sends to the receiver. The stored raw message is not changed.

## Execution settings

Chosen by the operator and applied as receiver environment on the deployment
side, not in this repository:

- near-market entries: maximum entry slippage 1%, no resting limit in the zone;
- leverage capped at 3x;
- risk of 1.50 USD per trade;
- margin cap raised to 25 USD so the per-pair minimum notionals on Binance
  (for example 50 USD for BTC and 20 USD for ETH, BCH and LINK) are reachable;
- Stop Target Hit and All targets achieved posts close the position.

## Receiver change

Every `aiohttp` session in the receiver is created with `trust_env=True`, so a
deployment can send venue traffic through an HTTPS proxy. This is needed where
the venue geoblocks the host. Without proxy environment variables nothing
changes.

## Consequences and follow-ups

- The Binance lane depends on the proxy being up. The mark-price guard fails
  closed, so a proxy outage blocks opens rather than skipping the check.
- The dashboard shows the bot as `altsignals-ft`, on the Binance futures path.
- The per-pair minimum notional is enforced by Freqtrade, not by the receiver.
- A possible later step is an LLM fallback restricted to the unparsed residue.
