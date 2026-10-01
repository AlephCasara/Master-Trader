# killers-bot — observer-only Telethon listener + local-LLM classifier + paper sim
# Container target for the Zeabur deployment (bd dp1.5): the observer runs as
# a service, the classifier subprocess is the env-driven mt-classify wrapper
# (endpoint/bearer/model from MT_CLASSIFY_*), and the Telethon session is
# seeded onto the state volume by the entrypoint — never baked in.
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# observer.py imports all five of these at module load; the gate json is
# loaded at Config() time. Missing any of them the container crash-loops.
COPY __init__.py /app/killers_bot/__init__.py
COPY classifier.py /app/killers_bot/classifier.py
COPY confidence_gate.py /app/killers_bot/confidence_gate.py
COPY confidence_gate.json /app/killers_bot/confidence_gate.json
COPY rules_classifier.py /app/killers_bot/rules_classifier.py
COPY simulator.py /app/killers_bot/simulator.py
COPY strict_open.py /app/killers_bot/strict_open.py
COPY observer.py /app/killers_bot/observer.py
COPY schema.sql /app/killers_bot/schema.sql

COPY mt-classify /usr/local/bin/mt-classify
COPY mt-observer-entrypoint.sh /usr/local/bin/mt-observer-entrypoint.sh
RUN chmod +x /usr/local/bin/mt-classify /usr/local/bin/mt-observer-entrypoint.sh

VOLUME ["/var/lib/killers"]

RUN useradd -u 1000 -m -s /sbin/nologin killers \
    && mkdir -p /var/lib/killers && chown killers:killers /var/lib/killers
USER killers

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    KILLERS_CLAUDE_BINARY=/usr/local/bin/mt-classify

ENTRYPOINT ["/usr/local/bin/mt-observer-entrypoint.sh"]
