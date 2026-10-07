"""Shared venue cost model (issue #1): fees, funding accrual, slippage."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ft_userdata.analysis import costs  # noqa: E402
from ft_userdata.analysis.costs import (  # noqa: E402
    FundingDataGap, FundingSeries, book_impact_bps, fee_bps, funding_cost, trade_costs,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def hourly(rates, start=T0):
    """Settlements on the hour starting at `start`."""
    return FundingSeries.from_pairs(
        (int((start + timedelta(hours=i)).timestamp() * 1000), r) for i, r in enumerate(rates))


def test_no_default_product_and_spot_is_not_futures():
    with pytest.raises(KeyError):
        costs.venue_product("binance", "futures-or-spot?")
    with pytest.raises(TypeError):
        costs.venue_product("binance")  # product is mandatory
    assert fee_bps("binance", "spot", "taker") == 10.0
    assert fee_bps("binance", "perp", "taker") == 5.0
    assert fee_bps("binance", "spot", "maker") == 5 * fee_bps("binance", "perp", "maker")


def test_hyperliquid_maker_is_a_fee_not_a_rebate():
    assert fee_bps("hyperliquid", "perp", "maker") == 1.5
    assert fee_bps("hyperliquid", "perp", "taker") == 4.5


def test_every_rate_carries_source_and_check_date_and_unknown_tier_fails():
    for vp in costs.SCHEDULES.values():
        tier = vp.tier(costs.BASE_TIER)
        assert tier.source and tier.checked == "2026-09-15"
        assert tier.maker_bps >= 0 and tier.taker_bps >= tier.maker_bps
    with pytest.raises(KeyError):
        fee_bps("hyperliquid", "perp", "taker", tier="vip9")


def test_funding_paid_only_for_settlements_inside_the_hold():
    s = hourly([0.0001] * 6)  # 00:00 .. 05:00
    # Opened and closed between two settlements: pays nothing.
    paid, n = funding_cost(s, "long", T0 + timedelta(minutes=10), T0 + timedelta(minutes=50), notional=1000)
    assert (paid, n) == (0.0, 0)
    # 00:30 -> 01:30 spans the 01:00 settlement only.
    paid, n = funding_cost(s, "long", T0 + timedelta(minutes=30), T0 + timedelta(minutes=90), notional=1000)
    assert n == 1 and paid == pytest.approx(0.1)
    # Boundary rule: open_ts < t <= close_ts.
    paid, n = funding_cost(s, "long", T0 + timedelta(hours=1), T0 + timedelta(hours=3), notional=1000)
    assert n == 2  # 02:00 and 03:00, not 01:00


def test_positive_funding_is_paid_by_longs_and_received_by_shorts():
    s = hourly([0.0002, -0.0001, 0.0003])
    long_paid, _ = funding_cost(s, "long", T0 - timedelta(minutes=1), T0 + timedelta(hours=2), notional=1000)
    short_paid, _ = funding_cost(s, "short", T0 - timedelta(minutes=1), T0 + timedelta(hours=2), notional=1000)
    assert long_paid == pytest.approx(0.4)
    assert short_paid == pytest.approx(-0.4)


def test_funding_on_marked_notional_uses_price_at_each_settlement():
    s = hourly([0.0001, 0.0001, 0.0001])
    prices = {T0 + timedelta(hours=1): 100.0, T0 + timedelta(hours=2): 120.0}
    paid, n = funding_cost(s, "short", T0, T0 + timedelta(hours=2), base_qty=10, price_at=prices.__getitem__)
    assert n == 2
    assert paid == pytest.approx(-(0.0001 * 10 * 100 + 0.0001 * 10 * 120))
    with pytest.raises(ValueError):
        funding_cost(s, "short", T0, T0 + timedelta(hours=2), base_qty=10)
    with pytest.raises(ValueError):
        funding_cost(s, "short", T0, T0 + timedelta(hours=2), notional=1, base_qty=1, price_at=float)


def test_coverage_gaps_and_ambiguous_timestamps_raise():
    s = hourly([0.0001] * 3)
    with pytest.raises(FundingDataGap):  # hold extends far past the data
        funding_cost(s, "long", T0, T0 + timedelta(hours=10), notional=1000, interval_h=1)
    holey = FundingSeries.from_pairs([(int(T0.timestamp() * 1000), 1e-4),
                                      (int((T0 + timedelta(hours=5)).timestamp() * 1000), 1e-4)])
    with pytest.raises(FundingDataGap):
        funding_cost(holey, "long", T0, T0 + timedelta(hours=5), notional=1000, interval_h=1)
    with pytest.raises(TypeError):
        funding_cost(s, "long", 1_725_000_000, T0, notional=1000)
    with pytest.raises(ValueError):
        funding_cost(s, "long", datetime(2026, 9, 1), T0, notional=1000)


def test_trade_costs_itemises_fees_slippage_and_funding():
    s = hourly([0.0001] * 40, start=T0 - timedelta(hours=2))
    c = trade_costs("hyperliquid", "perp", "BTC/USDC", "short", 1000.0,
                    T0 + timedelta(minutes=5), T0 + timedelta(hours=36, minutes=5), funding=s)
    assert c.entry_fee == pytest.approx(0.45) and c.exit_fee == pytest.approx(0.45)
    assert c.entry_slippage == pytest.approx(0.5) and c.exit_slippage == pytest.approx(0.5)
    assert c.funding_settlements == 36
    assert c.funding == pytest.approx(-36 * 0.1)  # short receives
    assert c.total == pytest.approx(0.45 * 2 + 0.5 * 2 - 3.6)
    assert c.as_dict()["total_bps"] == pytest.approx(c.total / 1000 * 1e4)
    assert c.fee_source.startswith("https://hyperliquid")


def test_maker_fills_pay_maker_fee_without_slippage_and_spot_has_no_funding():
    c = trade_costs("binance", "spot", "ETH/USDT", "long", 1000.0, T0, T0 + timedelta(days=3),
                    entry="maker", exit="taker", slippage_bps=8)
    assert c.entry_fee == pytest.approx(1.0) and c.entry_slippage == 0.0
    assert c.exit_fee == pytest.approx(1.0) and c.exit_slippage == pytest.approx(0.8)
    assert c.funding == 0.0 and c.funding_settlements == 0
    with pytest.raises(ValueError):
        trade_costs("binance", "spot", "ETH/USDT", "long", 1000.0, T0, T0, funding=hourly([0.0]))


def test_book_impact_walks_levels_and_reports_insufficient_depth():
    asks = [(100.0, 2.0), (101.0, 10.0)]
    assert book_impact_bps(asks, 150.0, "buy") == 0.0           # fills inside the touch
    cost = book_impact_bps(asks, 301.0, "buy")                    # 2 @100 + ~0.99 @101
    assert cost == pytest.approx(((301.0 / (2 + 101.0 / 101.0)) - 100.0) / 100.0 * 1e4)
    assert book_impact_bps(asks, 10_000.0, "buy") is None
    bids = [(99.0, 1.0), (98.0, 5.0)]
    assert book_impact_bps(bids, 99.0, "sell", reference=99.5) == pytest.approx(0.5 / 99.5 * 1e4)
    assert book_impact_bps(bids, 197.0, "sell") > 0


def test_load_funding_reads_the_repo_feather_schema(tmp_path):
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    folder = tmp_path / "hyperliquid" / "funding"
    folder.mkdir(parents=True)
    df = pd.DataFrame({"date": pd.date_range(T0, periods=4, freq="1h", tz="UTC"),
                       "funding_rate": [1e-5, 2e-5, 3e-5, 4e-5]})
    df.to_feather(folder / "BTC_USDT-funding.feather")
    series = costs.load_funding("hyperliquid", "BTC/USDC", data_dir=tmp_path)
    assert series.times_ms[0] == int(T0.timestamp() * 1000)
    assert series.rates == (1e-5, 2e-5, 3e-5, 4e-5)
    with pytest.raises(FileNotFoundError):
        costs.load_funding("hyperliquid", "DOGE/USDC", data_dir=tmp_path)
