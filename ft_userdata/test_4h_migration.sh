#!/bin/bash
# Test 4H timeframe migration for trend-following strategies
# Compares current 1h results vs 4h on the same pairs
#
# Usage: ./test_4h_migration.sh
#
# This script:
# 1. Downloads 4h candle data
# 2. Runs backtests on both 1h and 4h for Supertrend and MasterTraderV1
# 3. Compares profit, drawdown, and profit/DD ratio
#
# IMPORTANT: You need to temporarily change the strategy's timeframe to '4h'
# in the .py file for the 4h backtest, or create a 4h variant.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DAYS=180
TIMERANGE=$(python3 -c 'from datetime import datetime, timedelta, timezone; import sys; now = datetime.now(timezone.utc); days = int(sys.argv[1]); print(f"{now - timedelta(days=days):%Y%m%d}-{now:%Y%m%d}")' "$DAYS")
IMAGE="freqtradeorg/freqtrade:stable"
CONFIG="/freqtrade/user_data/configs/backtest-SupertrendStrategy.json"

echo "=== Downloading 4h data ==="
docker run --rm \
  -v "$SCRIPT_DIR/user_data:/freqtrade/user_data" \
  "$IMAGE" \
  download-data \
  --config "$CONFIG" \
  --timeframe 4h \
  --timerange "$TIMERANGE" \
  --days "$DAYS"

echo ""
echo "=== Backtesting SupertrendStrategy on 1h (current) ==="
docker run --rm \
  -v "$SCRIPT_DIR/user_data:/freqtrade/user_data" \
  "$IMAGE" \
  backtesting \
  --strategy SupertrendStrategy \
  --config "$CONFIG" \
  --timerange "$TIMERANGE" \
  --timeframe 1h \
  --disable-max-market-positions

echo ""
echo "=== Backtesting SupertrendStrategy on 4h ==="
echo "NOTE: To test 4h, temporarily change timeframe='4h' in SupertrendStrategy.py"
echo "Or create a SupertrendStrategy4H variant"
docker run --rm \
  -v "$SCRIPT_DIR/user_data:/freqtrade/user_data" \
  "$IMAGE" \
  backtesting \
  --strategy SupertrendStrategy \
  --config "$CONFIG" \
  --timerange "$TIMERANGE" \
  --timeframe 4h \
  --disable-max-market-positions

echo ""
echo "=== Compare results above ==="
echo "Key metrics to compare: Net Profit %, Max Drawdown %, Profit/DD Ratio, Trade Count"
echo "If 4h has better or comparable Profit/DD ratio, migrate."
