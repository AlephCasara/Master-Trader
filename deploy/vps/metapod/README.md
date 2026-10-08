# metapod-vps deployment (plain docker compose)

The 16-service dry-run fleet (12 fleet services + 4 test-lane bots) as one
compose project for metapod-vps, ported off Zeabur. Host facts and firewall
consequences live in `RUNTIME.md`; this directory is the deployable unit.

```
deploy/vps/metapod/
├── compose.yaml        # the fleet (this is the only committed artifact)
└── env/                # NOT committed; one file per service, seeded at deploy
```

## Getting the files onto the host

This directory carries no host addresses: the repo is public, so IPs,
tailnet names and the home-LLM endpoint are deployment values (see `.env`
below). The operator runbook holds the actual addresses. Automation (nixbox)
uses the key-based sshd on port 2222, never port 22 (Tailscale SSH intercepts
it):

```bash
rsync -e "ssh -p 2222" -a deploy/vps/metapod/ admin@<metapod-host>:~/metapod/
# env files carry secrets; copy them separately (bd k1u.3):
rsync -e "ssh -p 2222" -a env/ admin@<metapod-host>:~/metapod/env/

ssh -p 2222 admin@<metapod-host>
cd ~/metapod && docker compose pull && docker compose up -d
```

From nixbox you can also drive Docker directly:

```bash
DOCKER_HOST=ssh://admin@<metapod-host>:2222 docker compose -f deploy/vps/metapod/compose.yaml ps
```

Compose fails fast if an env file is missing, which is intentional: seed
`env/` and `.env` before `up`.

## Env files (secret-grade, never committed)

Every service except `hl-gateway` reads `./env/<service>.env`. The gateway
proxies public market data only and needs no secrets. `mt-observer` reads a
second file after its base file (later files win):

| Service | Files |
|---|---|
| all 10 bots | `env/<bot-name>.env` |
| `killers-receiver`, `insiders-receiver` | `env/<name>.env` |
| `ft-dashboard` | `env/ft-dashboard.env` |
| `mt-observer` | `env/mt-observer.env` + `env/mt-observer-chain.env` |
| `mt-relay` | `env/mt-relay.env` |
| `hl-gateway` | none |

Secrets carried there include the bots' `FREQTRADE__API_SERVER__*` trio,
`SIGNAL_RECEIVER_TOKEN`, the receivers' `KILLERS_INGRESS_TOKEN` and
`KILLERS_FT_USERNAME`/`KILLERS_FT_PASSWORD`, the dashboard's receiver tokens
and FT API credentials, the observer's Telegram session/tokens and
`MT_CLASSIFY_BEARER`, and the relay's `TS_AUTHKEY`. One non-secret key
matters here: the dashboard's env file from the Zeabur era may still set a
fleet DNS suffix. The compose `environment:` block forces `FLEET_DNS_SUFFIX=""`
(compose `environment:` overrides `env_file:`), so bare compose names always
win.

`MT_GEMINI_KEY` is not in any env file: it lives on the host at
`~/.config/mt-gemini/key` and is read from there (bd k1u.3).

## `.env` substitution values (never committed)

Three compose variables are filled from a `.env` file next to compose.yaml
(same seeding bead as the env files, bd k1u.3):

| Variable | Meaning |
|---|---|
| `MT_CLASSIFY_ENDPOINT` | full URL of the home-LLM completion endpoint |
| `MT_CLASSIFY_HOST` | its hostname, as used in the endpoint URL |
| `MT_CLASSIFY_HOST_IP` | the tailnet IP to pin that hostname to (`extra_hosts`) |

The pin exists because MagicDNS does not resolve inside containers and the
endpoint's certificate is issued for the FQDN, so the URL keeps the hostname
while `extra_hosts` maps it. The observer's channel IDs
(`KILLERS_TG_CHANNEL_ID`, `INSIDERS_TG_CHANNEL_ID`, `TRIAL_CHANNELS`)
identify the operator's signal sources and live in `env/mt-observer.env`,
not in the repo.

## Ports (tailnet-only by host firewall)

Published ports resolve only from the tailnet (the host's DOCKER-USER chain
drops everything else). Ports are published plainly: never bind `127.0.0.1`,
or tailnet DNAT stops working.

| Host port | Service | Host port | Service |
|---|---|---|---|
| 8000 | ft-dashboard | 8099 | ft-killers-scalp |
| 8080 | hl-gateway | 8102 | ft-oi-trend-pullback |
| 8089 | killers-receiver | 8103 | ft-short-keltner-hl-live |
| 8090 | insiders-receiver | 8105 | ft-test-bollinger |
| 8095 | ft-keltner-bounce | 8106 | ft-test-nasos |
| 8096 | ft-funding-fade | 8107 | ft-test-elliot |
| 8098 | ft-insiders-scalp | 8108 | ft-test-combined |

`mt-observer` and `mt-relay` publish nothing. Bots listen on container port
8080; the receivers on 8089; the dashboard on 8000.

## Image tags

All five fleet images default to the `20261007-1` migration-day GHCR builds.
Override per image without editing the file:

```bash
MT_FREQTRADE_TAG=20261001-3 MT_HL_GATEWAY_TAG=20261001-1 docker compose up -d
```

Variables: `MT_HL_GATEWAY_TAG`, `MT_FREQTRADE_TAG`,
`MT_KILLERS_RECEIVER_TAG`, `MT_DASHBOARD_TAG`, `MT_OBSERVER_TAG`. The live
Zeabur test lane ran older freqtrade builds (`20261001-3` / `20261002-1`);
exact tag parity is checked during cutover (bd k1u.8). Flip the defaults
there if parity with the old host matters more than freshness.

## mt-relay (pending bd k1u.4)

`mt-relay` (userspace `tailscale/tailscale`) is kept from the Zeabur fleet so
the dashboard keeps its `mt-dash` tailnet hostname. It needs a fresh
single-use `TS_AUTHKEY` in `env/mt-relay.env`; the key is consumed at first
login and the `relay_tsstate` volume keeps the session afterwards. Nothing in
the fleet waits on the relay (`depends_on` only orders startup, and
containerboot retries failed logins on its own), so a missing or invalid key
cannot block the rest of the stack: seed `env/mt-relay.env` last, or run
`docker compose up -d mt-relay` once the key arrives. Dropping the service
entirely is bead k1u.4's decision (the host itself is already on the tailnet,
so the dashboard is reachable at `http://<metapod-tailnet-ip>:8000` either
way).

`TS_KUBE_SECRET` from the Zeabur template was a kubernetes no-op and is not
ported. `cap_add: [NET_ADMIN]` is required by the tailscale image on plain
Docker.

## Zeabur-isms stripped

- Internal DNS suffix: gone everywhere; service-to-service URLs use bare
  compose names and the dashboard pins `FLEET_DNS_SUFFIX=""`.
- Template / `variable update` / `service exec` tooling: replaced by compose
  plus env files plus ssh.
- The freqtrade base image runs `ENTRYPOINT ["freqtrade"]`, so the per-bot
  run scripts override `entrypoint:` (never `command:`, which would append
  the script path as a freqtrade argument).
- `HOME=/home/ftuser` is baked into the mt-freqtrade image; nothing to set
  here.

## What is deliberately not here

- The headless/cloak browser container: a separate follow-up bead carries it
  (with its own `mem_limit` requirement; the host has 7.8 GB RAM + 3 GB
  swap).
- `ft-funding-refresh`, `ft-metrics-exporter`, `ft-prometheus`: the Zeabur
  fleet never ran them; they belong to the upstream VPS compose.
- `services/insiders-receiver/Dockerfile` (Node 20 + claude-code): unused by
  this fleet; the receiver runs the shared `mt-killers-receiver` image.

## Post-deploy checklist

1. `docker compose ps`: 16 containers, all healthy/running (the observer and
   relay have no healthcheck; check their logs), restart policies set.
2. From the tailnet: `curl http://<metapod-tailnet-ip>:8000/healthz` answers.
3. From a non-tailnet vantage: `nc -z <metapod-public-ip> 8000` times out
   (and 22, 8080, 8089 likewise).
4. Observer: one live test signal classified end to end; the receivers place
   the dry-run order.
5. Trade counts: the new databases match the pre-migration snapshot row for
   row (bd k1u.1's snapshot is the baseline).
