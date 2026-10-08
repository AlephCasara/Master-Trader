"""Production dashboard registry and account-group regression tests."""

import asyncio

import pytest

from app import FLEET_REGISTRY, _bot_links, _deployment_fleet


def _by_key():
    return {bot["key"]: bot for bot in FLEET_REGISTRY}


def test_dashboard_tracks_every_current_live_executor():
    bots = _by_key()
    assert set(bots) == {
        "fundingfade",
        "keltner",
        "oi-trend",
        "short-keltner-hl",
        "killers-ft",
        "insiders-ft",
        "altsignals-ft",
        # Test lane (2026-10-02): machinery-exercising bots, never epochs.
        "test-bollinger",
        "test-nasos",
        "test-elliot",
        "test-combined",
    }
    assert "cascade" not in bots


def test_short_keltner_points_to_fresh_live_epoch():
    bot = _by_key()["short-keltner-hl"]
    assert bot["url"] == "http://ft-short-keltner-hl-live:8080"
    assert bot["container"] == "ft-short-keltner-hl-live"
    assert bot["freqtrade_ui"] is None

    links = _bot_links(bot, [], [])
    assert links["freqtrade_ui"] is None
    assert links["logs_hint"] == "docker logs ft-short-keltner-hl-live --tail 50"


def test_shared_binance_wallet_has_one_account_group():
    bots = _by_key()
    for key in ("fundingfade", "keltner", "oi-trend"):
        assert bots[key]["account_group"] == "binance-spot"

    assert bots["killers-ft"]["account_group"] != "binance-spot"
    assert bots["insiders-ft"]["account_group"] != "binance-spot"
    assert bots["altsignals-ft"]["account_group"] != "binance-spot"
    assert bots["short-keltner-hl"]["account_group"] != "binance-spot"


def test_strategy_kinds_distinguish_copiers_from_autonomous_bots():
    bots = _by_key()
    assert bots["killers-ft"]["strategy_kind"] == "copy-trader"
    assert bots["insiders-ft"]["strategy_kind"] == "copy-trader"
    assert bots["altsignals-ft"]["strategy_kind"] == "copy-trader"
    assert bots["short-keltner-hl"]["strategy_kind"] == "autonomous-quant"
    assert bots["oi-trend"]["strategy_kind"] == "autonomous-quant"


def test_deployment_fleet_unset_keeps_the_full_registry(monkeypatch):
    monkeypatch.delenv("FLEET_BOTS", raising=False)
    assert _deployment_fleet(FLEET_REGISTRY) is FLEET_REGISTRY

    monkeypatch.setenv("FLEET_BOTS", "  ,  ")
    assert _deployment_fleet(FLEET_REGISTRY) is FLEET_REGISTRY


def test_deployment_fleet_scopes_to_the_declared_subset_in_order(monkeypatch):
    monkeypatch.setenv("FLEET_BOTS", "short-keltner-hl, killers-ft")
    scoped = _deployment_fleet(FLEET_REGISTRY)
    assert [bot["key"] for bot in scoped] == ["short-keltner-hl", "killers-ft"]


def test_deployment_fleet_rejects_unknown_keys_loudly(monkeypatch):
    monkeypatch.setenv("FLEET_BOTS", "short-keltner-hl, typo-bot")
    with pytest.raises(ValueError, match="typo-bot"):
        _deployment_fleet(FLEET_REGISTRY)


def test_altsignals_e_um_receiver_driven_binance_futures_dry_run():
    bot = _by_key()["altsignals-ft"]
    assert bot["account_group"] == "binance-altsignals"
    assert bot["venue"] == "binance"
    assert bot["receiver_url"] == "http://altsignals-receiver:8089"
    assert "lineage" not in bot


def test_altsignals_nunca_usa_os_caminhos_da_hyperliquid(monkeypatch):
    import app

    bot = _by_key()["altsignals-ft"]
    assert app._native_stop_verification(bot, [], [])["status"] == "not-applicable"

    chamadas = []

    async def fake_binance(pair, timeframe, limit, start_ms, end_ms):
        chamadas.append(pair)
        return {"source": "binance"}

    monkeypatch.setattr(app, "api_binance_candles", fake_binance)
    out = asyncio.run(app.api_trade_candles("altsignals-ft", "ETH/USDT:USDT"))
    assert out == {"source": "binance"}
    assert chamadas == ["ETH/USDT:USDT"]


def test_link_do_tradingview_de_futuros_binance_usa_o_perp():
    bot = _by_key()["altsignals-ft"]
    trade = {"pair": "ETH/USDT:USDT", "stake_amount": 10}
    links = _bot_links(bot, [trade], [])
    assert links["tradingview_top_pair"] == (
        "https://www.tradingview.com/chart/?symbol=BINANCE:ETHUSDT.P")
