"""
Metapod-vps compose conformance (bd k1u.2).

Locks the Zeabur-to-plain-compose port: the exact 16-service fleet, restart
policies, bare service names (no .zeabur.internal, FLEET_DNS_SUFFIX empty),
gateway dependency topology, the entrypoint-vs-command trap on the freqtrade
image, the host port map, the home-LLM pin on the observer, and the
userspace-tailscale relay without its k8s no-op secret. Committed artifacts
must carry no infrastructure literals (IPs, tailnet names, channel IDs):
they are deployment values in .env / env files, enforced below.
"""

import re
from pathlib import Path

import pytest
import yaml

COMPOSE_FILE = (
    Path(__file__).resolve().parents[1] / "deploy" / "vps" / "metapod" / "compose.yaml"
)

FLEET_SERVICES = {
    "hl-gateway",
    "ft-short-keltner-hl-live",
    "ft-keltner-bounce",
    "ft-funding-fade",
    "ft-oi-trend-pullback",
    "ft-killers-scalp",
    "ft-insiders-scalp",
    "killers-receiver",
    "insiders-receiver",
    "ft-dashboard",
    "mt-observer",
    "mt-relay",
    "ft-test-bollinger",
    "ft-test-nasos",
    "ft-test-elliot",
    "ft-test-combined",
}

BOT_ENTRYPOINTS = {
    "ft-short-keltner-hl-live": "mt-run-short-keltner.sh",
    "ft-keltner-bounce": "mt-run-keltner-bounce.sh",
    "ft-funding-fade": "mt-run-funding-fade.sh",
    "ft-oi-trend-pullback": "mt-run-oi-trend.sh",
    "ft-killers-scalp": "mt-run-killers-scalp.sh",
    "ft-insiders-scalp": "mt-run-insiders-scalp.sh",
    "ft-test-bollinger": "mt-run-test-bollinger.sh",
    "ft-test-nasos": "mt-run-test-nasos.sh",
    "ft-test-elliot": "mt-run-test-elliot.sh",
    "ft-test-combined": "mt-run-test-combined.sh",
}

# host port -> service; container ports are 8080 (bots, gateway), 8089
# (receivers) and 8000 (dashboard). Observer and relay publish nothing.
PUBLISHED_PORTS = {
    "8080": "hl-gateway",
    "8000": "ft-dashboard",
    "8089": "killers-receiver",
    "8090": "insiders-receiver",
    "8095": "ft-keltner-bounce",
    "8096": "ft-funding-fade",
    "8098": "ft-insiders-scalp",
    "8099": "ft-killers-scalp",
    "8102": "ft-oi-trend-pullback",
    "8103": "ft-short-keltner-hl-live",
    "8105": "ft-test-bollinger",
    "8106": "ft-test-nasos",
    "8107": "ft-test-elliot",
    "8108": "ft-test-combined",
}

HL_BOTS = {"ft-short-keltner-hl-live", "ft-killers-scalp", "ft-insiders-scalp"}


@pytest.fixture
def compose_content():
    return COMPOSE_FILE.read_text()


@pytest.fixture
def compose():
    return yaml.safe_load(COMPOSE_FILE.read_text())


@pytest.fixture
def services(compose):
    return compose["services"]


def test_compose_file_exists():
    assert COMPOSE_FILE.exists()


def test_exactly_the_sixteen_fleet_services(services):
    assert set(services) == FLEET_SERVICES


def test_every_service_restarts_unless_stopped(services):
    for name, svc in services.items():
        assert svc.get("restart") == "unless-stopped", (
            f"{name}: restart is {svc.get('restart')!r}"
        )


def test_no_zeabur_internal_dns_anywhere(compose_content):
    assert ".zeabur.internal" not in compose_content


def test_dashboard_forces_bare_container_names(services):
    assert services["ft-dashboard"]["environment"]["FLEET_DNS_SUFFIX"] == ""


def test_no_kube_secret_on_the_relay(services):
    for name, svc in services.items():
        assert "TS_KUBE_SECRET" not in svc.get("environment", {}), name


def test_hyperliquid_bots_wait_for_healthy_gateway(services):
    for name in HL_BOTS:
        assert services[name]["depends_on"] == {
            "hl-gateway": {"condition": "service_healthy"}
        }, name
    for name in set(BOT_ENTRYPOINTS) - HL_BOTS:
        assert "depends_on" not in services[name], (
            f"{name} talks to Binance directly; it must not gate on hl-gateway"
        )


def test_bot_scripts_override_entrypoint_not_command(services):
    """The mt-freqtrade image inherits ENTRYPOINT ["freqtrade"]; a compose
    `command:` would be appended as an argument to freqtrade and the run
    script would never execute."""
    for name, script in BOT_ENTRYPOINTS.items():
        svc = services[name]
        assert svc.get("entrypoint") == [f"/usr/local/bin/{script}"], name
        assert "command" not in svc, name


def test_published_host_ports_are_unique_and_match_the_map(services):
    seen = {}
    for name, svc in services.items():
        ports = svc.get("ports", [])
        if name in ("mt-observer", "mt-relay"):
            assert ports == [], f"{name} must not publish ports"
        for mapping in ports:
            # Quoted strings in the compose file; an unquoted `8080:8080`
            # would load as a map and silently break the split below.
            assert isinstance(mapping, str), f"{name}: quote port mappings"
            host_port, container_port = mapping.split(":")
            assert host_port not in seen, (
                f"host port {host_port} reused by {name} and {seen[host_port]}"
            )
            seen[host_port] = name
            expected_container = {
                "hl-gateway": "8080",
                "ft-dashboard": "8000",
                "killers-receiver": "8089",
                "insiders-receiver": "8089",
            }.get(name, "8080")
            assert container_port == expected_container, name
    assert seen == PUBLISHED_PORTS


def test_bots_reach_hyperliquid_only_through_the_gateway(services):
    for name, lane in (
        ("ft-short-keltner-hl-live", "short"),
        ("ft-killers-scalp", "killers"),
        ("ft-insiders-scalp", "insiders"),
    ):
        env = services[name]["environment"]
        assert env["FREQTRADE__EXCHANGE__CCXT_CONFIG__urls__api__public"] == (
            f"http://hl-gateway:8080/{lane}"
        )
        assert env["FREQTRADE__EXCHANGE__CCXT_CONFIG__urls__api__private"] == (
            f"http://hl-gateway:8080/{lane}"
        )


def test_signal_receiver_urls_point_at_the_bare_receiver_names(services):
    assert services["ft-killers-scalp"]["environment"]["SIGNAL_RECEIVER_URL"] == (
        "http://killers-receiver:8089"
    )
    assert services["ft-insiders-scalp"]["environment"]["SIGNAL_RECEIVER_URL"] == (
        "http://insiders-receiver:8089"
    )


def test_receivers_reach_the_gateway_only_through_their_bot(services):
    for bot, receiver, lane in (
        ("ft-killers-scalp", "killers-receiver", "killers"),
        ("ft-insiders-scalp", "insiders-receiver", "insiders"),
    ):
        svc = services[receiver]
        assert svc["environment"]["HYPERLIQUID_INFO_URL"] == (
            f"http://hl-gateway:8080/{lane}/info"
        )
        assert svc["environment"]["KILLERS_FT_BASE_URL"] == f"http://{bot}:8080"
        assert svc["depends_on"] == [bot]
        assert svc["volumes"] == [f"{receiver.replace('-', '_')}_state:/var/lib/killers"]


def test_observer_fanout_uses_bare_receiver_urls(services):
    env = services["mt-observer"]["environment"]
    assert env["KILLERS_RECEIVER_URL"] == "http://killers-receiver:8089/event"
    assert env["INSIDERS_RECEIVER_URL"] == "http://insiders-receiver:8089/event"


def test_observer_pins_the_home_llm_host_via_env_parameters(services):
    svc = services["mt-observer"]
    assert svc["extra_hosts"] == [
        "${MT_CLASSIFY_HOST:?set in .env}:${MT_CLASSIFY_HOST_IP:?set in .env}"
    ]
    assert svc["environment"]["MT_CLASSIFY_ENDPOINT"] == (
        "${MT_CLASSIFY_ENDPOINT:?set in .env}"
    )
    # The subprocess timeout must outlive the classify transport timeout so a
    # slow LLM reports a clean error class instead of SIGKILL.
    assert int(svc["environment"]["KILLERS_CLAUDE_TIMEOUT_SEC"]) > int(
        svc["environment"]["MT_CLASSIFY_TIMEOUT_SEC"]
    )


def test_observer_channel_ids_stay_out_of_the_repo(services):
    env = services["mt-observer"]["environment"]
    for key in ("KILLERS_TG_CHANNEL_ID", "INSIDERS_TG_CHANNEL_ID", "TRIAL_CHANNELS"):
        assert key not in env, f"{key} identifies the operator's signal sources"


IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# 127.0.0.1 is the loopback constant, not an infrastructure address.
IPV4_ALLOWLIST = {"127.0.0.1"}


def test_no_infrastructure_literals_in_committed_artifacts():
    for path in (COMPOSE_FILE, COMPOSE_FILE.parent / "README.md"):
        text = path.read_text()
        found = set(IPV4_RE.findall(text)) - IPV4_ALLOWLIST
        assert not found, f"{path.name}: host IPs are .env values, got {found}"
        assert ".ts.net" not in text, f"{path.name}: no tailnet names in the repo"


def test_observer_takes_base_env_then_chain_overlay(services):
    assert services["mt-observer"]["env_file"] == [
        "./env/mt-observer.env",
        "./env/mt-observer-chain.env",
    ]


def test_every_service_except_the_gateway_reads_its_own_env_file(services):
    for name, svc in services.items():
        if name == "hl-gateway":
            assert "env_file" not in svc, "the gateway is public-data only"
            continue
        expected = [f"./env/{name}.env"]
        if name == "mt-observer":
            expected.append("./env/mt-observer-chain.env")
        declared = svc["env_file"]
        if isinstance(declared, str):
            declared = [declared]
        assert declared == expected, name


def test_fleet_images_come_from_the_fork_registry(services):
    expected_repo = {
        "hl-gateway": "ghcr.io/alephcasara/mt-hl-gateway:",
        "ft-dashboard": "ghcr.io/alephcasara/mt-dashboard:",
        "mt-observer": "ghcr.io/alephcasara/mt-observer:",
        "killers-receiver": "ghcr.io/alephcasara/mt-killers-receiver:",
        "insiders-receiver": "ghcr.io/alephcasara/mt-killers-receiver:",
    }
    for name, svc in services.items():
        image = svc["image"]
        if name == "mt-relay":
            assert image == "tailscale/tailscale:v1.98.10", name
        elif name in expected_repo:
            assert image.startswith(expected_repo[name]), name
        else:
            assert image.startswith("ghcr.io/alephcasara/mt-freqtrade:${MT_FREQTRADE_TAG:-"), name


def test_relay_keeps_userspace_tailscale(services):
    svc = services["mt-relay"]
    env = svc["environment"]
    assert env["TS_HOSTNAME"] == "mt-dash"
    assert env["TS_USERSPACE"] == "true"
    assert env["TS_STATE_DIR"] == "/var/lib/tailscale"
    assert "NET_ADMIN" in svc["cap_add"]
    assert svc["volumes"] == ["relay_tsstate:/var/lib/tailscale"]


def test_every_service_has_a_memory_limit(services):
    expected = {
        "hl-gateway": "256M",
        "killers-receiver": "256M",
        "insiders-receiver": "256M",
        "ft-dashboard": "192M",
        "mt-observer": "1G",
        "mt-relay": "256M",
    }
    for name, svc in services.items():
        limit = svc["deploy"]["resources"]["limits"]["memory"]
        if name in expected:
            assert limit == expected[name], name
        else:
            assert limit == "2G", name


def test_bots_mount_their_own_user_data_volume(services):
    for name in BOT_ENTRYPOINTS:
        volume_name = name.replace("-", "_") + "_user_data"
        assert services[name]["volumes"] == [
            f"{volume_name}:/freqtrade/user_data"
        ], name
    for name in ("killers-receiver", "insiders-receiver", "mt-observer"):
        assert any(v.endswith(":/var/lib/killers") for v in services[name]["volumes"]), name
    assert services["ft-dashboard"]["volumes"] == [
        "dashboard_actions:/var/lib/dashboard-actions"
    ]
    for name in ("killers-receiver", "insiders-receiver", "mt-observer"):
        volume = services[name]["volumes"][0].split(":")[0]
        assert volume in yaml.safe_load(COMPOSE_FILE.read_text())["volumes"], name


def test_bots_curl_ping_and_python_services_probe_healthz(services):
    for name in BOT_ENTRYPOINTS:
        hc = services[name]["healthcheck"]
        assert "curl" in hc["test"], name
        assert "http://localhost:8080/api/v1/ping" in hc["test"], name
        assert hc["start_period"] == "120s", name
    for name, port in (
        ("hl-gateway", "8080"),
        ("ft-dashboard", "8000"),
        ("killers-receiver", "8089"),
        ("insiders-receiver", "8089"),
    ):
        hc = services[name]["healthcheck"]
        probe = " ".join(hc["test"])
        assert "urllib.request" in probe, name
        assert f"localhost:{port}/healthz" in probe, name


def test_container_names_match_service_names(services):
    for name, svc in services.items():
        assert svc["container_name"] == name
