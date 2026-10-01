#!/bin/sh
# ft-killers-scalp — Zeabur entrypoint (bd dp1.5), mirroring the compose
# entrypoint exactly except configs/strategies live baked at /opt/mt.
set -e
mkdir -p /freqtrade/user_data/logs
export FREQTRADE__DB_URL=sqlite:////freqtrade/user_data/tradesv3.dryrun.KillersScalpV1-local.sqlite
export FREQTRADE__ORDER_TYPES__STOPLOSS_ON_EXCHANGE=false
python /opt/mt/configs/guard_db_mode.py --config /opt/mt/configs/KillersScalpV1.json
sleep 60
exec freqtrade trade --logfile /freqtrade/user_data/logs/KillersScalpV1-local.log --config /opt/mt/configs/KillersScalpV1.json --strategy KillersScalpV1
