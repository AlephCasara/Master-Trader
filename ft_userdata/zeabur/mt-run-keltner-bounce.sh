#!/bin/sh
# ft-keltner-bounce — Zeabur entrypoint (bd dp1.5), mirroring the compose
# entrypoint exactly except configs/strategies live baked at /opt/mt.
set -e
mkdir -p /freqtrade/user_data/logs
export FREQTRADE__DB_URL=sqlite:////freqtrade/user_data/tradesv3.dryrun.KeltnerBounceV1-local.sqlite
python /opt/mt/configs/guard_db_mode.py --config /opt/mt/configs/KeltnerBounceV1.json
sleep 60
exec freqtrade trade --logfile /freqtrade/user_data/logs/KeltnerBounceV1-local.log --config /opt/mt/configs/KeltnerBounceV1.json --strategy-path /opt/mt/strategies --strategy KeltnerBounceV1
