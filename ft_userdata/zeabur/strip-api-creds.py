"""Blank api_server credentials from baked freqtrade configs (bd dp1.5).

The fleet image is PUBLIC; live credentials arrive via FREQTRADE__ env at
runtime (env overrides win over JSON). Skips anything that is not a strict
JSON object — configs dir also carries non-config JSONs.
"""
import json
import pathlib

for cfg in pathlib.Path("/opt/mt/configs").glob("*.json"):
    try:
        data = json.loads(cfg.read_text())
    except json.JSONDecodeError as exc:
        print(f"strip-api-creds: skip (not strict json): {cfg.name}: {exc}")
        continue
    if not isinstance(data, dict):
        print(f"strip-api-creds: skip (not an object): {cfg.name}")
        continue
    api = data.get("api_server")
    if isinstance(api, dict):
        changed = False
        for key in ("username", "password", "jwt_secret_key", "ws_token"):
            if api.get(key):
                api[key] = ""
                changed = True
        if changed:
            cfg.write_text(json.dumps(data, indent=2))
            print(f"strip-api-creds: blanked api_server creds in {cfg.name}")
