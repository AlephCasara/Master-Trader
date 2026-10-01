#!/bin/sh
# ft-oi-trend-pullback — Zeabur entrypoint (bd dp1.5), mirroring the compose
# entrypoint exactly except configs/strategies live baked at /opt/mt.
set -e
mkdir -p /freqtrade/user_data/logs
export FREQTRADE__DB_URL=sqlite:////freqtrade/user_data/tradesv3.dryrun.OITrendPullbackV1-local.sqlite
python /opt/mt/configs/guard_db_mode.py --config /opt/mt/configs/OITrendPullbackV1.live.json
sleep 80
exec freqtrade trade --logfile /freqtrade/user_data/logs/OITrendPullbackV1-local.log --config /opt/mt/configs/OITrendPullbackV1.live.json --strategy OITrendPullbackV1
