# Carry shadow correction, accounting v2

Research only: short Hyperliquid perpetual + matching Binance spot long.
The entry (40% trailing-24h annualized funding), exit (10% trailing-12h),
three-position limit, six-hour repost window and study fees remain unchanged.
No exchange order endpoint is called by this tracker or replay.

`tracker.py` is the version-controlled replacement for the previously VPS-only
shadow tracker. Deploy to the existing shadow directory alongside monitor.py.
`replay.py SOURCE NEW_OUTPUT` replays the archived hourly decision timestamps
from monitor.log, using archived BBO and settled funding. It requires a new
output directory and never writes the source archive. Run on the VPS.

Corrections:
- A paper fill must follow the current posting timestamp and precede expiration.
- Closing the short buys at the ask; closing spot sells at the bid.
- Quote lookup rejects future observations and stale latest quotes, and handles
  gzip archives across midnight. Funding is deduplicated and cut off at decision time.
- Weekly exposure clips each position to the reporting interval and includes
  still-open positions. Realized cash is not labeled weekly total return.
- Weekly annualized yields are unavailable until boundary valuations exist.
  First-post success among filled orders is not labeled an overall maker fill rate.
- Corrupt state fails visibly instead of silently resetting the paper history.

The replay reports completed-episode return on matched modeled capital-time,
and full-period provisioned-capital return only when there are no open episodes.
Annualization is descriptive, not a forecast. Additional 5/10 bps per fill are
sensitivity scenarios, not estimates of actual execution costs.

## Promotion remains blocked

The archived quote timestamps are batch-start timestamps, not atomic executable
cross-venue observations. Funding ingestion times were not recorded. Trade-through
is a fill proxy; top-of-book quantities are recorded but not enforced by this
legacy model. Fees and fixed funding notional are modeled, not actual account
charges; $1500 required capital per $1000 hedge is an unverified assumption.
Consequently a positive corrected result is not sufficient for a trading bot.

Before any funded executor: validate per-leg observation/arrival times and depth,
model same-base-quantity legs, funding on marked notional, actual account fees,
collateral and hedge-failure handling; then re-evaluate the original preregistered
criteria on fresh evidence. Keep the original archive and v1 results withdrawn.
Do not optimize thresholds against this correction replay.

`check_depth.py SOURCE REPLAY` screens archived top-of-book quantities against
the modeled same-base-quantity legs. Versioned results and input checksums are in
`results/`; the full source archive stays on the VPS. The corrected 2026-09-14
replay returns 9.20% annualized on modeled deployed capital, below the 12% hurdle,
and 20/36 legs fail the best-quote depth screen. No funded executor is justified.

## Execution realism, 2026-10-02 (issue #27)

`execution_realism.py SOURCE REPLAY [FORWARD_EPISODES] [--depth-table T]` audits
the archive offline (read-only, aggregates only) using the shared cost model
(`ft_userdata/analysis/costs.py`). `sample_public_depth.py` samples deeper public
books (HL 20 levels, Binance spot 100) from a host outside the fleet's HL IP
budget. Results: `results/2026-10-02-execution-realism.json`,
`results/2026-10-03-public-depth.json`. Ran over 1,966 recorded decisions
(07-13..10-02), the 9 replayed episodes and 3 forward fills (ONDO, UNI, ENA).

**Break-even.** The replay nets $27.64 over 36 modeled $1,000 fills. Extra cost
per fill that takes it to zero: **7.68 bps**. To the post-hoc 9%/yr hurdle:
**0.17 bps**. To the original 12%: already short (-2.34 bps).

**What the recorded conditions add** (each row against the modeled replay; net and
annualized return on matched deployed capital-time):

| item | effect | net | yr |
|---|---:|---:|---:|
| modeled (study fees: HL 1.5/4.5, Binance spot 7.5 bps) | | $27.64 | 9.20% |
| Binance spot at the base-tier 10 bps (`costs.py`) | -$4.50 | $23.14 | 7.70% |
| same, plus HL taker entry when the maker post does not fill | -$7.20 | $20.44 | 6.80% |
| beyond-touch impact, today's public books, 3 taker legs/episode | -$1.66 | $25.98 | 8.65% |
| funding on marked base quantity (HL mark as oracle proxy), path-dependent, $3.12 of it ZEC | +$3.99 | $31.63 | 10.53% |
| **base-tier fees + depth** | -$6.16 | $21.48 | 7.15% |
| **base-tier fees + depth + marked funding** | -$2.17 | $25.47 | 8.48% |

The 7.5 bps study fee implies Binance's BNB fee discount; whether the account pays
fees that way is not verified. Without it the replay is below 9% before any slippage.

**Depth.** The archive keeps HL levels 1-2 and Binance level 1 only, so it can
bound, not price, impact. At the modeled legs the touch was smaller than the
same-base quantity for 5/12 Binance entry buys, 6/9 HL exit buys and 5/9 Binance
exit sells. At recorded decision times for the 11 traded coins the touch held
>= $1,000 on 35% of HL asks, 57% of Binance asks, 59% of Binance bids. Today's
public books (12 samples, 2026-10-03 00:10-01:05 UTC) put the extra cost of a
$1,000 taker leg beyond the touch at 0-3.6 bps (per-coin medians; max 8 bps, EIGEN),
and every sampled $1,000 leg filled within the returned levels.

**Fill proxy and hedge timing.** Trade-through fills are selected on a rising HL
bid: the HL bid at the fill snapshot is a median 10.8 bps (p90 41.8) through the
posted ask, and the Binance mid moves a median -15.1 bps one minute before to
+15.7 bps one minute after the fill snapshot (unconditional 1-minute change:
median 0, p10/p90 -14/+14). The model buys the hedge at the fill snapshot. A hedge
placed at the true fill instant could be up to ~15 bps cheaper; one placed a minute
late ~15.7 bps dearer, about 0.26 bps per second of latency. That band is twice the
sign break-even and about ninety times the 9% headroom, and minute snapshots cannot
narrow it. Our $1,000 post was a median 2.5x the displayed ask queue at posting.

**Basis and quote timing.** Snapshot `ts` is the batch start: Binance is read in
one call, then HL l2Book coin by coin at 0.15 s pace. For the traded coins (poll
positions 13-40 of 40) that skew adds 3-9 bps of noise per snapshot (1-minute HL
sigma 9.5-20.8 bps, RTT 0.05-0.30 s). Mid basis (HL/Binance - 1) over the archive runs
p10 -29..-6, p50 -4..+4, p90 +10..+20 bps across those coins; executable entry basis
ranged -31..+30 bps and exit -48..+7 bps across episodes. HL quotes were missing from
1.7% (to 08-22) and 2.1% (08-23..09-13, the 429 period) of decision snapshots, 0% since.

**Funding availability.** 95 of ~78,600 coin-hour funding fetches failed (later
backfilled); none fell in the hour of a replayed signal or exit, so replay funding
timing matches what the live tracker could have known.

**Verdict.** Under measured costs the sign survives (base fees and depth use about
1.7 of the 7.68 bps per fill: 1.25 + 0.46), but the post-hoc 9% hurdle does not:
base-tier Binance fees alone take it to 7.70%, and with depth it is 7.15% (8.48% if
the ZEC-driven marked-funding gain is counted). Hedge timing, not depth, is the dominant
unknown. Criteria 2 and 3 of the preregistration stay unvalidated; no dry run.

### Request budget (proposal, not applied)

The monitor polls HL directly from the VPS IP, outside `services/hl-gateway`'s
budget (900/min for the fleet, leaving ~300 of HL's 1,200/min per IP). Its own load
is ~100 weight/min (`metaAndAssetCtxs` 20 + 40 x `l2Book` 2) and, in the minute
after each hour, a further ~840 (40 x `fundingHistory` at 20 + 1 per 20 rows): ~940
in the minute when the fleet refreshes candles. 3,150 l2Book/funding 429s were logged
07-13..09-13 (38% in minute :00): 677 before the 08-23 live cutover (~9-23/day), 2,473
after (~90-140/day). None since 09-14, when the gateway started (00:13) and the
monitor restarted (02:01). The burst still exceeds the gateway's non-fleet allowance and can push the fleet into
upstream 429s and the gateway's 10-second cooldown. The risk is load-dependent: one
gateway health read covering 00:58-01:03 UTC on 10-03 showed no faults at today's
light fleet load (72 weight in the last minute), while the 08-23..09-13 period with
six live bots is when the 429s occurred. Proposed:

1. Pace the hourly funding fetch to stay under ~250 weight/min (about 7 coins per
   minute from :01) and run the tracker once all 40 are in; keep the minute BBO
   at ~100/min, moved off second :00 (e.g. :20) away from candle-close bursts.
2. A token bucket in `monitor._http` capped at 250/min. Today a 429 is retried up to
   three more times at 2/4/8 s, i.e. inside the same burst; back off past the minute.
3. Longer term, run the monitor on the compose network as a gateway observation
   client (tier 2), so one process owns the IP budget.
4. While touching the monitor, record what #27 needs: request start/end times per
   leg and HL l2Book's `time`, and the HL levels already returned (up to $3,000
   notional, no extra weight). Binance depth only for pending/open/candidate coins.

None of these change the frozen strategy rules; deploying them is an operator call.
