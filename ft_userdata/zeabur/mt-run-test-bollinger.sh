#!/bin/sh
# ft-test-bollinger — busy test-lane bot (bd test lane): dry-run only,
# machinery exercising. ~5 trades/day expected (2026-10-02 backtest round 2).
set -e
mkdir -p /freqtrade/user_data/logs
export FREQTRADE__DB_URL=sqlite:////freqtrade/user_data/tradesv3.dryrun.TestBollingerRSI.sqlite
python /opt/mt/configs/guard_db_mode.py --config /opt/mt/configs/TestBollingerRSI.json
sleep 90
exec freqtrade trade --logfile /freqtrade/user_data/logs/TestBollingerRSI.log --config /opt/mt/configs/TestBollingerRSI.json --strategy-path /opt/mt/strategies --strategy BollingerRSIMeanReversion
