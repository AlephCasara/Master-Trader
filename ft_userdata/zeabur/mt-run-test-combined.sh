#!/bin/sh
# ft-test-combined — test-lane bot (bd test lane round 2): dry-run only, CombinedBinHAndCluc (config written this round).
set -e
mkdir -p /freqtrade/user_data/logs
export FREQTRADE__DB_URL=sqlite:////freqtrade/user_data/tradesv3.dryrun.TestCombinedBinHAndCluc.sqlite
python /opt/mt/configs/guard_db_mode.py --config /opt/mt/configs/TestCombinedBinHAndCluc.json
sleep 140
exec freqtrade trade --logfile /freqtrade/user_data/logs/TestCombinedBinHAndCluc.log --config /opt/mt/configs/TestCombinedBinHAndCluc.json --strategy-path /opt/mt/strategies --strategy CombinedBinHAndCluc
