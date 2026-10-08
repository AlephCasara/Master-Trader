"""Production dashboard registry and account-group regression tests."""

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
