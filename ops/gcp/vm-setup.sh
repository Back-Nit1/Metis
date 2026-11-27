#!/usr/bin/env bash
# Runs ON the VM as root (invoked by deploy.sh). Idempotent: re-run to redeploy.
# Installs the release tarball at /tmp/metis.tar.gz and (re)starts three
# loopback-only services. Nothing listens on a routable interface.
set -euo pipefail
: "${HOST:?set HOST (<ip>.sslip.io)}"
: "${METIS_BUILD:?set METIS_BUILD to the deployed git commit}"

APP=/opt/metis
DATA=/var/lib/metis
MODELS=/opt/models
MODEL_ID=small.en
MODEL_REPO=Systran/faster-whisper-small.en

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends libsndfile1 ca-certificates curl caddy make

# Google Compute Engine's rsyslog rule forwards daemon and kernel messages to
# /dev/console. Ubuntu runs rsyslog as the unprivileged `syslog` user, while
# /dev/console is writable only by root:tty. Without this supplementary group,
# rsyslog retries the failed output every 30 seconds and floods the journal.
# Preserve serial-console diagnostics instead of disabling the vendor rule.
if id syslog >/dev/null 2>&1 && getent group tty >/dev/null 2>&1; then
  usermod -aG tty syslog
  systemctl restart rsyslog
fi

id metis >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin metis
mkdir -p "$DATA" "$MODELS"

command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

rm -rf "$APP" && mkdir -p "$APP"
tar -xzf /tmp/metis.tar.gz -C "$APP"
cd "$APP"
# System python 3.12 (Ubuntu 24.04) so the venv is readable by the metis user.
uv sync --all-packages --no-dev --extra asr --frozen --python /usr/bin/python3.12

# Model weights are fetched once, at deploy time, pinned to the commit resolved
# here. The transcriber runs local_files_only and never downloads per request.
REV_FILE="$MODELS/$MODEL_ID.revision"
if [ ! -f "$REV_FILE" ]; then
  .venv/bin/python - "$MODEL_REPO" "$MODELS/$MODEL_ID" "$REV_FILE" <<'PY'
import pathlib, sys
from huggingface_hub import HfApi, snapshot_download

repo, target, rev_file = sys.argv[1:]
sha = HfApi().model_info(repo).sha
snapshot_download(repo_id=repo, revision=sha, local_dir=target)
pathlib.Path(rev_file).write_text(sha)
PY
fi

cat >/etc/metis.env <<EOF
METIS_MODE=local
METIS_ENV=dev
METIS_BUILD=$METIS_BUILD
METIS_ALLOW_FIXTURE_IDENTITY=false
METIS_FIXTURE_USER_ID=demo_authenticated_subject
METIS_DESKTOP_IDENTITY_PATH=$DATA/install_id
METIS_ALLOWED_HOSTS=$HOST
METIS_SQLITE_PATH=$DATA/metis.sqlite
METIS_LOCAL_OBJECTS_DIR=$DATA/objects
METIS_CORS_ORIGINS=tauri://localhost,http://tauri.localhost,https://tauri.localhost
METIS_SPEECH_ENGINE=remote
METIS_SPEECH_ENDPOINT=http://127.0.0.1:8787
METIS_SPEECH_CPU_THREADS=2
METIS_SPEECH_MODEL_DIR=$MODELS
METIS_SPEECH_MODEL_ID=$MODEL_ID
METIS_SPEECH_ARTIFACT_REVISION=$(cat "$REV_FILE")
HF_HUB_OFFLINE=1
EOF

# Single-user access key. With METIS_DESKTOP_AUTH_SECRET set the API rejects any
# request whose bearer is not exactly this value. The file is a required
# EnvironmentFile and fixture identity is off, so a missing key stops the
# services instead of silently opening the API.
[ -f /etc/metis-api.env ] || (
  umask 077
  echo "METIS_DESKTOP_AUTH_SECRET=$(head -c 36 /dev/urandom | base64 | tr -d '=+/\n')" >/etc/metis-api.env
)

# Dictation ("Flow") ASR server: loopback-only, bearer secret generated once.
[ -f /etc/metis-speech.env ] || (
  umask 077
  echo "METIS_SPEECH_SECRET=$(head -c 32 /dev/urandom | base64 | tr -d '=+/\n')" >/etc/metis-speech.env
)

unit() { # name, ExecStart
  cat >"/etc/systemd/system/$1.service" <<EOF
[Unit]
Description=$1
After=network.target

[Service]
User=metis
WorkingDirectory=$APP
EnvironmentFile=/etc/metis.env
EnvironmentFile=-/etc/metis-speech.env
EnvironmentFile=/etc/metis-api.env
ExecStart=$2
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$DATA
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  # Unit files contain configuration paths, not secret values. Keep their mode
  # consistent even when a previous install created one under a restrictive
  # umask; systemd otherwise emits a warning on every daemon reload.
  chmod 0644 "/etc/systemd/system/$1.service"
}
unit metis-api "$APP/.venv/bin/uvicorn metis_api.main:app --host 127.0.0.1 --port 8000"
unit metis-changes "$APP/.venv/bin/uvicorn metis_api.main:app --host 127.0.0.1 --port 8001"
unit metis-timers "$APP/.venv/bin/python -m metis_pipeline.local_worker --mode timers --processes 1"
unit metis-worker "$APP/.venv/bin/python -m metis_pipeline.local_worker"
unit metis-speech "$APP/.venv/bin/python -m metis_speech.server --port 8787"

# Keep the API's global concurrency ceiling separate from its per-client
# sliding-window request limit. A drop-in avoids shell interpolation in
# ExecStart and can be rolled out without replacing the rest of the unit.
install -D -m 0644 "$APP/ops/gcp/systemd/metis-api-limits.conf" \
  /etc/systemd/system/metis-api.service.d/limits.conf
install -D -m 0644 "$APP/ops/gcp/systemd/metis-changes-limits.conf" \
  /etc/systemd/system/metis-changes.service.d/limits.conf
"$APP/ops/gcp/configure-runtime.sh"

# A bucket must be provisioned first by configure-backups.sh. An existing
# backup configuration survives redeploys; credentials come from VM identity.
if [ -n "${METIS_BACKUP_BUCKET:-}" ]; then
  [[ "$METIS_BACKUP_BUCKET" =~ ^[a-z0-9][a-z0-9._-]+[a-z0-9]$ ]] || exit 2
  printf 'METIS_BACKUP_BUCKET=%s\n' "$METIS_BACKUP_BUCKET" >/etc/metis-backup.env
  chmod 0600 /etc/metis-backup.env
fi
if [ -f /etc/metis-backup.env ]; then
  install -d -m 0700 -o metis -g metis /var/backups/metis
  install -m 0644 "$APP/ops/gcp/systemd/metis-backup.service" /etc/systemd/system/
  install -m 0644 "$APP/ops/gcp/systemd/metis-backup.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable -q --now metis-backup.timer
fi
install -D -m 0755 "$APP/ops/gcp/monitoring/queue-observer.py" \
  /usr/local/libexec/metis-queue-observer
install -m 0644 "$APP/ops/gcp/systemd/metis-queue-observer.service" \
  /etc/systemd/system/metis-queue-observer.service
install -m 0644 "$APP/ops/gcp/systemd/metis-queue-observer.timer" \
  /etc/systemd/system/metis-queue-observer.timer

# Public edge: TLS + health-checked load balancing across the configured API
# endpoints. A single-VM install defaults to the loopback API. Additional
# private API nodes can be supplied as comma- or space-separated host:port
# entries in METIS_API_UPSTREAMS when invoking this script.
HOST="$HOST" \
  METIS_API_UPSTREAMS="${METIS_API_UPSTREAMS:-127.0.0.1:8000}" \
  METIS_CHANGES_UPSTREAMS="${METIS_CHANGES_UPSTREAMS:-127.0.0.1:8001}" \
  "$APP/ops/gcp/render-caddyfile.sh" >/etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable -q caddy
systemctl reload-or-restart caddy

chown -R metis:metis "$DATA"
chown -R root:root "$APP" "$MODELS" && chmod -R a+rX "$APP" "$MODELS"
systemctl daemon-reload
systemctl enable -q metis-api metis-changes metis-worker metis-timers metis-speech
systemctl enable -q --now metis-queue-observer.timer
systemctl restart metis-speech metis-api metis-changes metis-worker metis-timers
if [ -f /etc/systemd/system/metis-web.service ]; then
  systemctl disable --now metis-web
fi

# Host CPU/memory metrics plus systemd journal logs. The VM service account is
# provisioned by configure-monitoring.sh with only metric/log writer roles.
if ! dpkg-query -W -f='${Status}' google-cloud-ops-agent 2>/dev/null | grep -q 'install ok installed'; then
  curl -fsSLo /tmp/add-google-cloud-ops-agent-repo.sh \
    https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
  bash /tmp/add-google-cloud-ops-agent-repo.sh --also-install
fi
install -m 0644 "$APP/ops/gcp/monitoring/ops-agent.yaml" \
  /etc/google-cloud-ops-agent/config.yaml
systemctl enable -q google-cloud-ops-agent
systemctl restart google-cloud-ops-agent

KEY="$(sed -n 's/^METIS_DESKTOP_AUTH_SECRET=//p' /etc/metis-api.env)"
for _ in $(seq 30); do
  curl -fsS -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/v1/health && echo && exit 0
  sleep 1
done
journalctl -u metis-api -n 40 --no-pager
exit 1
