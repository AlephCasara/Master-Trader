#!/bin/sh
# FundingFade forward test (#109, prereg ff-forward-v2-vs-no-protections-2026-10-01).
# Run on the VPS from a checkout of this repo: research/ff_forward_v2c/run_forward.sh [START] [END]
# Read-only towards production: copies funding feathers and live trades out of
# ft-funding-fade, never writes to it. Backtests run in throwaway containers.
set -eu
START=${1:-20261001}
END=${2:-}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=${REPO:-$(git -C "$HERE" rev-parse --show-toplevel)}
WORK=${WORK:-/home/ubuntu/master-trader/research/ff_forward_v2c_runs}
SEED=/home/ubuntu/master-trader/research/ff_roi_2026-09-30/user_data/data
IMAGE=freqtradeorg/freqtrade@sha256:50720a4af314a812be2cfbf5cc6331c63e9332b06f3f4372241f54bc61a35486
PINNED_COMMIT=f913795
PINNED_MD5=f4f70fb0a7c434bc96a2bf75382692fa

mkdir -p "$WORK/user_data/strategies" "$WORK/cfg"
[ -d "$WORK/user_data/data" ] || cp -a "$SEED" "$WORK/user_data/data"

# Strategy under test: the live file as frozen at registration.
git -C "$REPO" fetch -q origin main
git -C "$REPO" show "$PINNED_COMMIT:ft_userdata/user_data/strategies/FundingFadeV1.py" \
  > "$WORK/user_data/strategies/FundingFadeV1.py"
echo "$PINNED_MD5  $WORK/user_data/strategies/FundingFadeV1.py" | md5sum -c -
cp "$HERE/FFForward.py" "$WORK/user_data/strategies/"
cp "$HERE/bt.json" "$WORK/cfg/bt.json"

# Funding feathers as live saw them (read-only copy).
docker cp ft-funding-fade:/freqtrade/user_data/data/binance/funding/. "$WORK/user_data/data/binance/funding/"

PAIRS=$(python3 -c "import json;print(' '.join(json.load(open('$HERE/bt.json'))['exchange']['pair_whitelist']+['BTC/USDT']))")
nice -n 10 docker run --rm --cpus 2 --memory 6g \
  -v "$WORK/user_data:/freqtrade/user_data" -v "$WORK/cfg:/cfg:ro" "$IMAGE" \
  download-data --config /cfg/bt.json --pairs $PAIRS --timeframes 1m 1h --timerange 20260901-

nice -n 10 docker run --rm --cpus 2 --memory 10g \
  -v "$WORK/user_data:/freqtrade/user_data" -v "$WORK/cfg:/cfg:ro" "$IMAGE" \
  backtesting --config /cfg/bt.json --strategy-list FFF_V2 FFF_V2_noC \
  --timerange "$START-$END" --timeframe-detail 1m --fee 0.00075 --enable-protections \
  --export trades --notes ff_forward_v2c --cache none

# Live V2 trades for calibration (read-only).
docker exec -i ft-funding-fade python3 - > "$WORK/live_trades.json" <<'PY'
import json, sqlite3
c = sqlite3.connect("file:/freqtrade/user_data/tradesv3.live.FundingFadeV1.v2.sqlite?mode=ro", uri=True)
rows = c.execute("select pair, open_date from trades").fetchall()
print(json.dumps([{"pair": p, "open_date": str(o).replace(" ", "T")} for p, o in rows]))
PY

LAST=$(python3 -c "import json;print(json.load(open('$WORK/user_data/backtest_results/.last_result.json'))['latest_backtest'])")
python3 "$HERE/compare.py" "$WORK/user_data/backtest_results/$LAST" \
  "$(echo "$START" | sed 's/\(....\)\(..\)\(..\)/\1-\2-\3/')" "$WORK/live_trades.json" \
  | tee "$WORK/result_$(date -u +%Y%m%dT%H%MZ).json"
