#!/bin/sh
# Operator-run wrapper for hl_funds.py (#123). Uses the Hyperliquid SDK and
# Python runtime installed on this Mac on 2026-08-23; override with HL_SDK / HL_PY.
HERE=$(cd "$(dirname "$0")" && pwd)
HL_PY=${HL_PY:-/Users/palmer/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3}
HL_SDK=${HL_SDK:-/Users/palmer/.cache/master-trader/hl-sdk}
PYTHONPATH="$HL_SDK" exec "$HL_PY" "$HERE/hl_funds.py" "$@"
