"""Venue-aware trading costs for signal-level research (issue #1).

One importable home for fee schedules, funding accrual and slippage so ad-hoc
studies report net outcomes instead of retyping constants.

    from ft_userdata.analysis.costs import trade_costs
    c = trade_costs("hyperliquid", "perp", "BTC/USDC", "short", 1000.0,
                    open_ts, close_ts)          # tz-aware datetimes
    c.as_dict()  # entry/exit fees, entry/exit slippage, funding, total

Design decisions (recorded on issue #1, 2026-09-15):

* Costs are keyed on (venue, product) with NO default product. Binance spot
  costs five times Binance USDT-M maker/taker at the base tier, so a caller must
  say which one it means. Products are "spot" and "perp" (perpetual futures;
  Binance "perp" means USDT-M).
* Fee rates are the base tier, each with the URL it was read from and the date.
  The caller passes ``tier`` explicitly; the module never guesses the account's
  actual tier. Hyperliquid's base maker fee is +0.015%, a fee, not a rebate.
* Funding integrates real settled rates over the holding period. A settlement
  at time t is paid by a position open over it: ``open_ts < t <= close_ts``.
  A position opened and closed between two settlements pays nothing. Positive
  rates are paid by longs to shorts. Settlement times come from the data as-is
  (Hyperliquid's carry offsets from the hour of a few ms, rarely minutes). Funding notional is either a fixed
  ``notional`` or ``base_qty`` x a caller-supplied price at each settlement
  (Hyperliquid settles on the oracle notional, Binance on the mark).
* Slippage is an explicit parameter in bps per taker fill. The default prior
  (5 bps) is the constant that kbt_sim.py and cost_ceiling.py already used; it
  is an assumption, not a measurement. Maker fills are charged no slippage,
  which ignores queue position and adverse selection. ``book_impact_bps``
  measures impact from a recorded order book when one is available.
* Partial fills are out of scope.

Boundary: Freqtrade-native backtest P&L does not use this module (no config
under user_data/configs sets ``fee``), so the two will not reconcile.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Sequence

BASE_TIER = "base"
DEFAULT_SLIPPAGE_BPS = 5.0
LIQUIDITY = ("maker", "taker")
SIDES = ("long", "short")
DEFAULT_FUNDING_DIR = Path(__file__).resolve().parents[1] / "user_data" / "data"


@dataclass(frozen=True)
class FeeTier:
    maker_bps: float
    taker_bps: float
    source: str
    checked: str  # ISO date the schedule was read


@dataclass(frozen=True)
class VenueProduct:
    venue: str
    product: str
    tiers: dict = field(hash=False)
    # Nominal spacing between funding settlements; None means no funding.
    funding_interval_h: float | None
    funding_notional: str | None  # which price the venue applies the rate to
    note: str = ""

    def tier(self, name: str) -> FeeTier:
        try:
            return self.tiers[name]
        except KeyError:
            raise KeyError(
                f"{self.venue}/{self.product} has no fee tier {name!r}; "
                f"known: {sorted(self.tiers)}") from None


# The only fee constants in this module. Base tier, no VIP/token discounts,
# checked 2026-09-15 (issue #1 comment). Add a tier only with its own source.
SCHEDULES: dict[tuple[str, str], VenueProduct] = {
    ("binance", "spot"): VenueProduct(
        "binance", "spot",
        {BASE_TIER: FeeTier(10.0, 10.0, "https://www.binance.com/en/fee/schedule", "2026-09-15")},
        funding_interval_h=None, funding_notional=None,
        note="Regular user. No BNB fee discount assumed."),
    ("binance", "perp"): VenueProduct(
        "binance", "perp",
        {BASE_TIER: FeeTier(2.0, 5.0, "https://www.binance.com/en/fee/futureFee", "2026-09-15")},
        funding_interval_h=8.0, funding_notional="mark",
        note="USDT-M perpetual, VIP0. Some symbols settle every 4h; settlement "
             "times come from the funding data, not from this interval."),
    ("hyperliquid", "perp"): VenueProduct(
        "hyperliquid", "perp",
        {BASE_TIER: FeeTier(1.5, 4.5, "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees", "2026-09-15")},
        funding_interval_h=1.0, funding_notional="oracle",
        note="Tier 0. Maker is a positive fee; rebates need >0.5% of exchange "
             "maker volume and are not a base rate. Re-tiers daily on 14-day volume."),
    ("weex", "perp"): VenueProduct(
        "weex", "perp",
        {BASE_TIER: FeeTier(2.0, 8.0, "WEEX help centre article 4415852872601", "2026-09-15")},
        funding_interval_h=8.0, funding_notional="mark"),
}


def venue_product(venue: str, product: str) -> VenueProduct:
    """Return the schedule for (venue, product). There is no default product."""
    key = (str(venue).lower(), str(product).lower())
    if key not in SCHEDULES:
        raise KeyError(f"No cost schedule for venue={venue!r} product={product!r}; "
                       f"known: {sorted(SCHEDULES)}")
    return SCHEDULES[key]


def fee_bps(venue: str, product: str, liquidity: str, tier: str = BASE_TIER) -> float:
    if liquidity not in LIQUIDITY:
        raise ValueError(f"liquidity must be one of {LIQUIDITY}, got {liquidity!r}")
    t = venue_product(venue, product).tier(tier)
    return t.maker_bps if liquidity == "maker" else t.taker_bps


def fee_fraction(venue: str, product: str, liquidity: str, tier: str = BASE_TIER) -> float:
    """Fee as a fraction of notional, e.g. 0.0005 for 5 bps."""
    return fee_bps(venue, product, liquidity, tier) / 1e4


# ---------------------------------------------------------------------------
# time handling: tz-aware datetimes in, epoch milliseconds internally
# ---------------------------------------------------------------------------
def _ms(ts) -> int:
    if isinstance(ts, bool) or isinstance(ts, (int, float)):
        raise TypeError("Pass a tz-aware datetime; bare numbers are ambiguous (s vs ms)")
    if not isinstance(ts, datetime):  # pandas.Timestamp is a datetime subclass
        raise TypeError(f"Expected a tz-aware datetime, got {type(ts).__name__}")
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("Naive datetime: pass a tz-aware (UTC) timestamp")
    return int(round(ts.timestamp() * 1000))


class FundingDataGap(ValueError):
    """Settled funding data does not cover the requested holding period."""


@dataclass(frozen=True)
class FundingSeries:
    """Settled funding rates as sorted (epoch_ms, rate) pairs, one venue/coin."""
    times_ms: tuple
    rates: tuple

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[int, float]]) -> "FundingSeries":
        dedup = {}
        for t, r in pairs:
            t, r = int(t), float(r)
            if not math.isfinite(r):
                continue
            dedup[t] = r
        items = sorted(dedup.items())
        return cls(tuple(t for t, _ in items), tuple(r for _, r in items))

    @classmethod
    def from_frame(cls, df) -> "FundingSeries":
        """From the repo's feather schema: columns [date (tz-aware), funding_rate]."""
        dates = df["date"]
        if getattr(dates.dt, "tz", None) is None:
            raise ValueError("Funding frame dates must be tz-aware UTC")
        ms = [_ms(ts) for ts in dates.dt.tz_convert("UTC")]
        return cls.from_pairs(zip(ms, df["funding_rate"].astype(float).tolist()))

    def window(self, open_ms: int, close_ms: int) -> list[tuple[int, float]]:
        """Settlements t with open_ms < t <= close_ms."""
        a = bisect.bisect_right(self.times_ms, open_ms)
        b = bisect.bisect_right(self.times_ms, close_ms)
        return list(zip(self.times_ms[a:b], self.rates[a:b]))


def check_coverage(series: FundingSeries, open_ms: int, close_ms: int,
                   interval_h: float) -> None:
    """Raise FundingDataGap unless settlements span the holding period without
    holes longer than 1.5 nominal intervals."""
    if close_ms < open_ms:
        raise ValueError("close_ts precedes open_ts")
    tol = int(1.5 * interval_h * 3_600_000)
    if not series.times_ms:
        raise FundingDataGap("No funding data")
    if series.times_ms[0] > open_ms + tol or series.times_ms[-1] < close_ms - tol:
        raise FundingDataGap("Funding data does not span the holding period")
    a = max(bisect.bisect_right(series.times_ms, open_ms) - 1, 0)
    b = bisect.bisect_right(series.times_ms, close_ms)
    times = series.times_ms[a:b + 1]
    for prev, nxt in zip(times, times[1:]):
        if nxt - prev > tol:
            raise FundingDataGap(f"Funding gap of {(nxt - prev) / 3.6e6:.1f}h inside the holding period")


def funding_cost(series: FundingSeries, side: str, open_ts: datetime, close_ts: datetime, *,
                 notional: float | None = None, base_qty: float | None = None,
                 price_at: Callable[[datetime], float] | None = None,
                 interval_h: float | None = None) -> tuple[float, int]:
    """Funding PAID over the hold (negative = received) and the settlement count.

    Exactly one of ``notional`` (fixed USD) or ``base_qty`` + ``price_at`` must
    be given. ``price_at`` receives each settlement time as a tz-aware datetime
    and should return the venue's funding price (HL oracle, Binance mark).
    When ``interval_h`` is given, the data must cover the window (see
    check_coverage); otherwise missing data silently means zero funding.
    """
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")
    if (notional is None) == (base_qty is None):
        raise ValueError("Pass exactly one of notional or base_qty")
    if base_qty is not None and price_at is None:
        raise ValueError("base_qty needs price_at to mark each settlement")
    open_ms, close_ms = _ms(open_ts), _ms(close_ts)
    if interval_h is not None:
        check_coverage(series, open_ms, close_ms, interval_h)
    sign = 1.0 if side == "long" else -1.0
    paid = 0.0
    window = series.window(open_ms, close_ms)
    for t, rate in window:
        if notional is not None:
            n = notional
        else:
            n = base_qty * float(price_at(datetime.fromtimestamp(t / 1000, tz=timezone.utc)))
        paid += sign * rate * n
    return paid, len(window)


@lru_cache(maxsize=64)
def _load_funding_cached(path: str) -> FundingSeries:
    import pandas as pd  # pyarrow needed only when reading feathers
    return FundingSeries.from_frame(pd.read_feather(path))


def funding_path(venue: str, pair: str, data_dir: Path | str | None = None) -> Path:
    """Resolve <data_dir>/<venue>/funding/<BASE>_<QUOTE>-funding.feather.

    The Hyperliquid downloader names files after the requested pair (often
    BTC_USDT), so a unique <BASE>_*-funding.feather match is accepted too."""
    root = Path(data_dir) if data_dir else DEFAULT_FUNDING_DIR
    folder = root / venue.lower() / "funding"
    base, _, quote = pair.replace(":", "/").partition("/")
    exact = folder / f"{base}_{quote.split('/')[0]}-funding.feather"
    if exact.exists():
        return exact
    matches = sorted(folder.glob(f"{base}_*-funding.feather"))
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(
        f"No unique funding file for {venue} {pair} in {folder} "
        f"(run ft_userdata/download_hyperliquid_funding.py or download_funding_rates.py)")


def load_funding(venue: str, pair: str, data_dir: Path | str | None = None) -> FundingSeries:
    return _load_funding_cached(str(funding_path(venue, pair, data_dir)))


# ---------------------------------------------------------------------------
# slippage
# ---------------------------------------------------------------------------
def book_impact_bps(levels: Sequence[tuple[float, float]], notional: float, side: str,
                    reference: float | None = None) -> float | None:
    """Average-price cost, in bps, of taking ``notional`` (quote currency)
    from one side of a book.

    ``side`` is "buy" (walks the asks; ``levels`` ascending) or "sell" (walks
    the bids; ``levels`` descending). ``levels`` are (price, base_size), best
    first. Cost is measured against ``reference``: by default the best level,
    i.e. impact beyond the touch; pass the mid to include the half-spread.
    Positive = worse than the reference. Returns None when the recorded
    levels cannot fill the whole notional.
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
    if notional <= 0:
        raise ValueError("notional must be positive")
    if not levels:
        return None
    ref = float(reference) if reference is not None else float(levels[0][0])
    remaining, qty, spent = float(notional), 0.0, 0.0
    for px, sz in levels:
        px, sz = float(px), float(sz)
        if px <= 0 or sz <= 0:
            continue
        take = min(sz, remaining / px)
        qty += take
        spent += take * px
        remaining -= take * px
        if remaining <= 1e-9 * notional:
            avg = spent / qty
            diff = avg - ref if side == "buy" else ref - avg
            return diff / ref * 1e4
    return None


# ---------------------------------------------------------------------------
# itemised trade cost
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CostBreakdown:
    venue: str
    product: str
    tier: str
    pair: str
    side: str
    notional: float
    entry_fee: float
    exit_fee: float
    entry_slippage: float
    exit_slippage: float
    funding: float
    funding_settlements: int
    slippage_bps: float
    fee_source: str
    fee_checked: str

    @property
    def total(self) -> float:
        return (self.entry_fee + self.exit_fee + self.entry_slippage
                + self.exit_slippage + self.funding)

    @property
    def total_bps(self) -> float:
        return self.total / self.notional * 1e4

    def as_dict(self) -> dict:
        d = asdict(self)
        d["total"] = self.total
        d["total_bps"] = self.total_bps
        return d


def trade_costs(venue: str, product: str, pair: str, side: str, notional: float,
                open_ts: datetime, close_ts: datetime, *,
                entry: str = "taker", exit: str = "taker", tier: str = BASE_TIER,
                slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
                exit_notional: float | None = None,
                funding: FundingSeries | None = None,
                funding_data_dir: Path | str | None = None,
                base_qty: float | None = None,
                price_at: Callable[[datetime], float] | None = None) -> CostBreakdown:
    """Itemised round-trip cost in quote currency; positive = cost.

    Fees use ``notional`` on entry and ``exit_notional`` (default: the same)
    on exit. Slippage is ``slippage_bps`` per taker fill and zero per maker
    fill. Perp funding uses ``funding`` if given, else the downloaded series
    for (venue, pair); data must cover the hold. Funding notional is fixed at
    ``notional`` unless ``base_qty`` and ``price_at`` are supplied.
    """
    vp = venue_product(venue, product)
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")
    if notional <= 0:
        raise ValueError("notional must be positive")
    if slippage_bps < 0:
        raise ValueError("slippage_bps must be >= 0")
    t = vp.tier(tier)
    exit_n = notional if exit_notional is None else float(exit_notional)

    def fee(liq, n):
        return n * fee_bps(venue, product, liq, tier) / 1e4

    def slip(liq, n):
        return n * slippage_bps / 1e4 if liq == "taker" else 0.0

    if vp.funding_interval_h is None:
        if funding is not None:
            raise ValueError(f"{venue}/{product} has no funding; do not pass a series")
        fund, n_settle = 0.0, 0
    else:
        series = funding if funding is not None else load_funding(venue, pair, funding_data_dir)
        kwargs = ({"base_qty": base_qty, "price_at": price_at} if base_qty is not None
                  else {"notional": notional})
        fund, n_settle = funding_cost(series, side, open_ts, close_ts,
                                      interval_h=vp.funding_interval_h, **kwargs)
    return CostBreakdown(
        venue=vp.venue, product=vp.product, tier=tier, pair=pair, side=side,
        notional=float(notional),
        entry_fee=fee(entry, notional), exit_fee=fee(exit, exit_n),
        entry_slippage=slip(entry, notional), exit_slippage=slip(exit, exit_n),
        funding=fund, funding_settlements=n_settle, slippage_bps=float(slippage_bps),
        fee_source=t.source, fee_checked=t.checked)
