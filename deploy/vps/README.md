# VPS deployment — Elder Brain (Oracle Cloud, sa-saopaulo-1)

Host: `ubuntu@100.96.225.124` (tailnet) / `159.112.191.120` (public).
Single canonical project root: `/home/ubuntu/master-trader/`.

## Layout

```
/home/ubuntu/master-trader/
├── README.md                       # written at bootstrap, see below
├── runtime -> /etc/dokploy/compose/compose-bypass-mobile-port-fbk1m6/code
│                                   # Dokploy-managed git checkout (vps-deploy branch)
├── research/                       # 9 GB OHLCV + backtest outputs (rsynced from Mac)
│   ├── data/, logs/, models/, hyperopt_results/, backtest_results/
│   ├── databases/, notebooks/, fear_greed_history.json
│   └── analysis/, meta_labeling/, engine_results/, evolution/
├── state/                          # health-report state + logs (writable, persistent)
│   ├── user_data/                  # FT_DIR/user_data (DB_DIR placeholder; unused on VPS)
│   ├── logs/                       # FT_DIR/logs
│   └── health_report_state.json
└── run-health-report.sh            # cron wrapper, source: deploy/vps/run-health-report.sh
```

Live runtime is owned by Dokploy (UI: `compose-bypass-mobile-port-fbk1m6`).
Containers: `ft-keltner-bounce` (8095), `ft-funding-fade` (8096),
`ft-funding-refresh`, `ft-grafana`, `ft-prometheus`, `ft-metrics-exporter`,
`ft-grafana-bridge`. All bound to `127.0.0.1`; tailnet-only access.

Live trade DB lives in docker volume
`compose-bypass-mobile-port-fbk1m6_ft_user_data` (NOT in `runtime/`).

## Bootstrap a fresh VPS

After Dokploy is up and the master-trader compose deployed:

```bash
ssh ubuntu@<host>
mkdir -p /home/ubuntu/master-trader/{research,state}
ln -sfn /etc/dokploy/compose/<compose-id>/code /home/ubuntu/master-trader/runtime

# Drop wrapper + cron
cp ~/Work/Dev/master-trader/deploy/vps/run-health-report.sh \
   /home/ubuntu/master-trader/run-health-report.sh
chmod +x /home/ubuntu/master-trader/run-health-report.sh
( crontab -l 2>/dev/null | grep -v master-trader/run-health-report
  echo '0 23 * * * /home/ubuntu/master-trader/run-health-report.sh'
) | crontab -

# Rsync research data from prior host (skip if starting fresh)
# rsync -a old-host:/home/ubuntu/master-trader/research/ /home/ubuntu/master-trader/research/
```

## Health report

`run-health-report.sh` runs `strategy_health_report.py` from `runtime/ft_userdata/`
(Dokploy-managed code, auto-updates on git push). It pulls rotated REST creds
from `ft-keltner-bounce` via `docker exec`. It posts to trade-webhook on the host
loopback, `WEBHOOK_URL=http://localhost:8088/freqtrade/event`. That is also the
default of every report script that posts to Telegram: they all go through
`ft_userdata/webhook_notify.py`, which reads `WEBHOOK_URL` and sends JSON.

Test on demand:
```bash
/home/ubuntu/master-trader/run-health-report.sh --stdout
```

## Mac side

`~/Work/Dev/master-trader/` holds code + tracked configs/strategies only (~20 MB).
All bulk research data lives only on VPS; backtests run on VPS.

## Backups

`backup_live_dbs.py` (#99) backs up the state that exists only on this host:
every `tradesv3.*.sqlite` in the `ft_user_data` volume (live and dry-run bots),
every `*.sqlite` in `killers_receiver_state` and `insiders_receiver_state`, the
exporter's `*.json` (circuit-breaker peak) and the observer's
`/home/ubuntu/killers-bot/state.sqlite`. The receiver databases matter most:
they tie each open position to its source signal, posted stop and targets, and
cannot be rebuilt from the exchange.

Each database is opened read-only and copied with SQLite's online backup API,
which takes a consistent snapshot without stopping the bots. The copy has to
pass `PRAGMA integrity_check` and is then gzipped. One run produces
`/home/ubuntu/backups/master-trader/<UTC stamp>/` with a `MANIFEST.json`
(source, size, sha256). Retention: every run from 14 days, the newest per ISO
week for 8 weeks, never fewer than 7. A failure exits non-zero and posts a
`[db-backup] WARN` line through trade-webhook.

What existed before (checked read-only on 2026-10-02): no scheduled backup of
any of this. Dokploy has no backup, volume backup or destination configured.
The host crontabs back up only another project (`rlm-db-backup.sh`).
`ft_userdata/automation_scheduler.sh`'s 02:00 line reads `~/ft_userdata`, which
holds no live database, and is not installed. The only copies are one-off
copies taken before risky changes. A cloud-level boot-volume backup policy
can't be seen from the host and is unverified.

Install, as root, because docker's volume directories are root-only:

```bash
# Check what it will copy (reads nothing but directory listings):
sudo /usr/bin/python3 /etc/dokploy/compose/compose-bypass-mobile-port-fbk1m6/code/deploy/vps/backup_live_dbs.py --list
# First run by hand, then confirm the directory:
sudo /usr/bin/python3 /etc/dokploy/compose/compose-bypass-mobile-port-fbk1m6/code/deploy/vps/backup_live_dbs.py
sudo ls /home/ubuntu/backups/master-trader/
# Daily at 03:15 UTC, clear of rlm's 02:30 and the 23:00 health report:
( sudo crontab -l 2>/dev/null | grep -v backup_live_dbs.py
  echo '15 3 * * * /usr/bin/python3 /etc/dokploy/compose/compose-bypass-mobile-port-fbk1m6/code/deploy/vps/backup_live_dbs.py >> /var/log/master-trader-backup.log 2>&1'
) | sudo crontab -
```

Point root's cron at the real, root-owned Dokploy path, not at
`/home/ubuntu/master-trader/runtime`. That symlink lives in a directory
`ubuntu` owns, so re-pointing it would make root run something else.

Restore a file (a Tier 5 change in
[UPDATING-WITHOUT-BREAKING-BOTS](../../docs/ops/UPDATING-WITHOUT-BREAKING-BOTS.md):
stop the owning container first, and never touch a live bot's database with
open positions without draining):

```bash
sudo -i
cd /home/ubuntu/backups/master-trader/<stamp>
gunzip -c killers_receiver_state/receiver-hyperliquid.sqlite.gz > /tmp/restore.sqlite
sha256sum /tmp/restore.sqlite      # compare with MANIFEST.json
sqlite3 /tmp/restore.sqlite 'PRAGMA integrity_check'
# with the container stopped: remove the target's -wal/-shm, copy the file in,
# chown 1000:1000 (the volume's owner), start the container.
```

These copies sit on the same disk as the data. They cover corruption, a bad
migration or an accidental delete, but not losing the host or its volume. An
off-host copy (a pull over the tailnet, or object storage) is still to be
decided.
