#!/bin/sh
# ft-funding-fade — Zeabur entrypoint (bd dp1.5). Mirrors the compose
# entrypoint (configs baked at /opt/mt) PLUS the hourly funding-refresh loop
# from the standalone ft-funding-refresh container, merged in because Zeabur
# volumes are per-service: the feathers must live in THIS bot's volume.
# The script resolves USER_DATA = script_dir/user_data -> /freqtrade/user_data,
# which is exactly this bot's volume.
set -e
mkdir -p /freqtrade/user_data/logs
export FREQTRADE__DB_URL=sqlite:////freqtrade/user_data/tradesv3.dryrun.FundingFadeV1-local.sqlite

(
  while true; do
    echo "[funding-refresh] $(date -u +%FT%TZ) starting incremental refresh"
    attempt=1
    for backoff in 5 30 120; do
      if python3 -u /freqtrade/download_funding_rates.py --incremental; then
        break
      fi
      echo "[funding-refresh] attempt $attempt failed; retrying in ${backoff}s"
      sleep "$backoff"
      attempt=$((attempt + 1))
    done
    now=$(date -u +%s)
    slot=$(( now / 3600 * 3600 + 600 ))
    if [ "$now" -ge "$slot" ]; then slot=$(( slot + 3600 )); fi
    echo "[funding-refresh] $(date -u +%FT%TZ) sleeping until next :10 ($(( slot - now ))s)"
    sleep $(( slot - now ))
  done
) &

python /opt/mt/configs/guard_db_mode.py --config /opt/mt/configs/FundingFadeV1.live.json
sleep 70
exec freqtrade trade --logfile /freqtrade/user_data/logs/FundingFadeV1-local.log --config /opt/mt/configs/FundingFadeV1.live.json --strategy FundingFadeV1
