#!/bin/sh
# ft-test-nfi — test-lane bot (bd test lane round 2): dry-run only, recovered NostalgiaForInfinityX6, modest 10-pair list.
set -e
mkdir -p /freqtrade/user_data/logs
export FREQTRADE__DB_URL=sqlite:////freqtrade/user_data/tradesv3.dryrun.TestNFIx6.sqlite
python /opt/mt/configs/guard_db_mode.py --config /opt/mt/configs/TestNFIx6.json
sleep 180
exec freqtrade trade --logfile /freqtrade/user_data/logs/TestNFIx6.log --config /opt/mt/configs/TestNFIx6.json --strategy-path /opt/mt/strategies --strategy NostalgiaForInfinityX6
