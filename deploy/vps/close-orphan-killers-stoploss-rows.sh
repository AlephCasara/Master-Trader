#!/usr/bin/env bash
# Mark Killers stop-order rows Hyperliquid already removed as canceled (#148).
#
# Freqtrade leaves the native stop `open` in its DB when the last TP limit
# closes a trade, because Hyperliquid has already dropped the reduce-only
# stop and the cancel fails. No exchange order is involved; this only fixes
# the DB row, the same way Freqtrade's own restart does. Runs the Python
# inside ft-killers-scalp as ftuser (WAL database). Read-only without --yes.
#
#   deploy/vps/close-orphan-killers-stoploss-rows.sh             # dry run
#   deploy/vps/close-orphan-killers-stoploss-rows.sh --yes 36 42 45
#
# Run it yourself; the agent does not.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
exec docker exec -i ft-killers-scalp python3 - "$@" < "$HERE/close_orphan_stoploss_rows.py"
