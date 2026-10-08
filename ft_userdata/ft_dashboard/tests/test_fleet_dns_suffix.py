"""FLEET_DNS_SUFFIX behavior (bd dp1.5, Zeabur deployment).

The same dashboard image must serve the compose deployment (empty suffix,
container-name DNS) and a Zeabur project (.zeabur.internal internal DNS):
the suffix rewrites every registry url/receiver_url host, and credential
lookups stay keyed by the registry host (suffix stripped).
"""
import importlib

import app


def _reload(monkeypatch, suffix):
    monkeypatch.setenv("FLEET_DNS_SUFFIX", suffix)
    return importlib.reload(app)


def test_empty_suffix_keeps_compose_urls(monkeypatch):
    mod = _reload(monkeypatch, "")
    for bot in mod.BOTS:
        url = bot.get("url") or ""
        assert ".zeabur.internal" not in url


def test_suffix_rewrites_every_url_and_receiver_url(monkeypatch):
    mod = _reload(monkeypatch, ".zeabur.internal")
    assert mod.BOTS, "registry must not be empty"
    for bot in mod.BOTS:
        for key in ("url", "receiver_url"):
            raw = bot.get(key)
            if not raw:
                continue
            assert raw.startswith("http://"), raw
            host = raw.split("//", 1)[1].rsplit(":", 1)[0]
            assert host.endswith(".zeabur.internal"), raw
            # The registry (bare) host must be recoverable by suffix removal —
            # the auth lookup depends on it.
            assert host.removesuffix(".zeabur.internal") in mod.SERVICE_API_SLUGS or \
                host.removesuffix(".zeabur.internal").startswith(("killers", "insiders", "altsignals", "ft-test")), raw


def test_api_auth_strips_suffix_for_slug_lookup(monkeypatch):
    mod = _reload(monkeypatch, ".zeabur.internal")
    bot = next(b for b in mod.BOTS if b.get("url"))
    user, password = mod._api_auth(bot["url"])
    # Falls through to the fleet pair (no per-bot slug vars set) — the point
    # is that it RESOLVES rather than KeyError-ing on the suffixed host.
    assert isinstance(user, str) and isinstance(password, str)
