# Killers open-risk monitor

`risk_warden.py` checks the open Killers book against the sizing contract and
alerts on changes. It has **no write path** to Freqtrade. Entry admission is
enforced by the receiver (`KILLERS_MAX_OPEN`).

`KILLERS_MAX_OPEN` counts every receiver position in `open`/`requested`,
including accepted limit entries still resting in the zone (up to the 24h
entry timeout). Five resting orders therefore block new signals until one
fills or expires — a resting entry can still fill, so it is reserved risk. Behaviour and env: see the
module docstring. History: #105 (it used to force-close positions measured
from the current mark, against a stale Binance-era DB copy).

It ships in the killers-receiver image (`/app/warden`) and uses that
container's env and DB. Host crontab (user `ubuntu`) on the VPS:

```
*/5 * * * * docker exec killers-receiver python3 /app/warden/risk_warden.py >> /home/ubuntu/killers-warden.log 2>&1
```

Install the cron line only after the receiver image containing `/app/warden`
is running. The former wrapper `/home/ubuntu/killers-warden/run-warden.sh`
(and its `receiver.sqlite` copy) is retired; a backup remains in that directory.
