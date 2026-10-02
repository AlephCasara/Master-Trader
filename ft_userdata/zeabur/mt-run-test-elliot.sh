#!/bin/sh
# ft-test-elliot — test-lane bot (bd test lane round 2): dry-run only, ElliotV5 community strategy.
set -e
mkdir -p /freqtrade/user_data/logs
export FREQTRADE__DB_URL=sqlite:////freqtrade/user_data/tradesv3.dryrun.TestElliotV5.sqlite
python /opt/mt/configs/guard_db_mode.py --config /opt/mt/configs/TestElliotV5.json
sleep 100
exec freqtrade trade --logfile /freqtrade/user_data/logs/TestElliotV5.log --config /opt/mt/configs/TestElliotV5.json --strategy-path /opt/mt/strategies --strategy ElliotV5
