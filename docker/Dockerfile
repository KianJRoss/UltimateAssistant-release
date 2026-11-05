FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/opt/assistant-home LOCALAPPDATA=/data/assistant \
    ULTIMATE_ASSISTANT_FILES_ROOT=/data/files
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --home-dir /opt/assistant-home --uid 10001 assistant \
    && mkdir -p /data/assistant /data/files /data/router/vault /data/herald/vault /app/apps/assistant \
    && chmod 700 /data/router/vault /data/herald/vault \
    && chown -R assistant:assistant /data /app
WORKDIR /app/apps/assistant
COPY apps/assistant/requirements.txt apps/assistant/herald.json ./
COPY docker/ /tmp/herald-install/
RUN python /tmp/herald-install/install-herald.py herald.json \
    && pip install --no-cache-dir -r requirements.txt
COPY --chown=assistant:assistant apps/assistant/ultimate_assistant ./ultimate_assistant
COPY --chown=assistant:assistant apps/assistant/VERSION ./VERSION
COPY LICENSE /usr/share/doc/ultimate-assistant/LICENSE
COPY apps/assistant/THIRD_PARTY_NOTICES.md /usr/share/doc/ultimate-assistant/THIRD_PARTY_NOTICES.md
USER assistant
