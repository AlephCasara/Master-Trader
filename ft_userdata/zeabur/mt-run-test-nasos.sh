#!/bin/sh
# ft-test-nasos — test-lane bot (bd test lane): dry-run only, calm cadence.
set -e
mkdir -p /freqtrade/user_data/logs
export FREQTRADE__DB_URL=sqlite:////freqtrade/user_data/tradesv3.dryrun.TestNASOSv5.sqlite
python /opt/mt/configs/guard_db_mode.py --config /opt/mt/configs/TestNASOSv5.json
sleep 100
exec freqtrade trade --logfile /freqtrade/user_data/logs/TestNASOSv5.log --config /opt/mt/configs/TestNASOSv5.json --strategy-path /opt/mt/strategies --strategy NASOSv5
