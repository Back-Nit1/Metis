#!/usr/bin/env bash
# Run ON the VM after deploying the hosted implementation from main.
# This explicit cutover preserves old single-user data; it does not assign it
# to the first person who signs in. See hosted-desktop.md for migration.
set -euo pipefail
APP="${APP:-/opt/metis}"
: "${METIS_FIREBASE_PROJECT_ID:?set the Identity Platform project ID}"
: "${METIS_LOCAL_LLM_MODEL:?set an installed server Ollama inference model}"
: "${METIS_LOCAL_EMBED_MODEL:?set an installed server Ollama embedding model}"
export METIS_FIREBASE_PROJECT_ID METIS_LOCAL_LLM_MODEL METIS_LOCAL_EMBED_MODEL
export METIS_FLOW_MODEL="${METIS_FLOW_MODEL-$METIS_LOCAL_LLM_MODEL}"
config_file="$(mktemp)"
trap 'rm -f "$config_file"' EXIT
python3 "$APP/ops/gcp/render-hosted-env.py" >"$config_file"
# Validate runtime prerequisites before changing authentication or restarting.
python3 - <<'PY'
import json, os, urllib.request
with urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=5) as response:
    models = {m['name'] for m in json.load(response)['models']}
for key in ('METIS_LOCAL_LLM_MODEL', 'METIS_LOCAL_EMBED_MODEL', 'METIS_FLOW_MODEL'):
    value = os.environ[key]
    if value not in models and value + ':latest' not in models:
        raise SystemExit(f'{key}: model is not installed on this server')
PY
"$APP/.venv/bin/python" -c 'from metis_contracts.config import Mode; assert Mode.SERVER.value == "server"'
install -m 0600 "$config_file" /etc/metis-hosted.env
for service in api changes worker timers speech; do
  install -d -m 0755 "/etc/systemd/system/metis-$service.service.d"
  cat >"/etc/systemd/system/metis-$service.service.d/zz-hosted.conf" <<'UNIT'
[Service]
# Appended after base EnvironmentFiles: clears the legacy shared identity.
EnvironmentFile=/etc/metis-hosted.env
UNIT
done
# EnvironmentFile values override Environment=, so give the decoder its own
# final override instead of letting it inherit the API's remote-client engine.
printf 'METIS_SPEECH_ENGINE=faster_whisper\n' >/etc/metis-hosted-speech.env
chmod 0600 /etc/metis-hosted-speech.env
printf 'EnvironmentFile=/etc/metis-hosted-speech.env\n' >>/etc/systemd/system/metis-speech.service.d/zz-hosted.conf
systemctl daemon-reload
systemctl restart metis-speech metis-api metis-changes metis-worker metis-timers
for _ in $(seq 30); do
  if curl -fsS http://127.0.0.1:8000/v1/health; then
    printf '\nHosted mode started. Verify two distinct accounts before rollout.\n'
    exit 0
  fi
  sleep 1
done
echo 'Hosted API did not become healthy. Inspect systemd service logs.' >&2
exit 1
