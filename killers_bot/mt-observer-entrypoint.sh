#!/bin/sh
# Observer container entrypoint (bd dp1.5). The Telethon session never ships
# in the image: it arrives as KILLERS_TG_SESSION_B64 and lands on the state
# volume, chmod 600. A session already present on the volume wins — the env
# var is only the initial seed, so a rotated-out value cannot clobber a
# session the running client has kept alive.
set -e

STATE_DIR=/var/lib/killers
SESSION_PATH=${KILLERS_TG_SESSION:-$STATE_DIR/killers.session}

mkdir -p "$STATE_DIR" "$(dirname "$SESSION_PATH")"

if [ -n "${KILLERS_TG_SESSION_B64:-}" ] && [ ! -f "$SESSION_PATH" ]; then
    printf '%s' "$KILLERS_TG_SESSION_B64" | base64 -d > "$SESSION_PATH"
    chmod 600 "$SESSION_PATH"
    echo "[entrypoint] session seeded from env onto $SESSION_PATH"
fi

if [ ! -f "$SESSION_PATH" ]; then
    echo "[entrypoint] FATAL: no session at $SESSION_PATH and no KILLERS_TG_SESSION_B64" >&2
    exit 1
fi

exec python3 -u -m killers_bot.observer
