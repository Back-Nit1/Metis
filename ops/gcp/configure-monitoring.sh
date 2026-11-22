#!/usr/bin/env bash
# Idempotently create VM telemetry, uptime, alert policies, and a dashboard.
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT to the GCP project id}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-$REGION-a}"
VM="${VM:-metis}"
ADDRESS="${ADDRESS:-$VM}"
OBS_SA_ID="${OBS_SA_ID:-metis-vm-observability}"
OBS_SA="$OBS_SA_ID@$PROJECT.iam.gserviceaccount.com"
GCP_DIR="$(cd "$(dirname "$0")" && pwd)"

g() { gcloud --quiet --project "$PROJECT" "$@"; }

g services enable logging.googleapis.com monitoring.googleapis.com

if ! g iam service-accounts describe "$OBS_SA" >/dev/null 2>&1; then
  g iam service-accounts create "$OBS_SA_ID" \
    --display-name="Metis VM observability"
fi
for role in roles/logging.logWriter roles/monitoring.metricWriter; do
  for attempt in $(seq 1 10); do
    if g projects add-iam-policy-binding "$PROJECT" \
      --member="serviceAccount:$OBS_SA" --role="$role" --condition=None >/dev/null; then
      break
    fi
    if [ "$attempt" -eq 10 ]; then
      echo "service account IAM binding did not propagate after 10 attempts" >&2
      exit 1
    fi
    sleep 3
  done
done

instance_id="$(g compute instances describe "$VM" --zone="$ZONE" --format='value(id)')"
ip="$(g compute addresses describe "$ADDRESS" --region="$REGION" --format='value(address)')"
host="${ip//./-}.sslip.io"

uptime_name="$(g monitoring uptime list-configs \
  --filter='displayName="Metis API HTTPS"' --format='value(name)' --limit=1)"
if [ -z "$uptime_name" ]; then
  uptime_name="$(g monitoring uptime create 'Metis API HTTPS' \
    --resource-type=uptime-url \
    --resource-labels="host=$host,project_id=$PROJECT" \
    --protocol=https --port=443 --path=/v1/health \
    --request-method=get --validate-ssl=true --status-classes=2xx \
    --period=1 --timeout=10 --format='value(name)')"
fi
uptime_id="${uptime_name##*/}"

error_filter='resource.type="gce_instance" AND (jsonPayload._SYSTEMD_UNIT="metis-api.service" OR jsonPayload._SYSTEMD_UNIT="metis-worker.service" OR jsonPayload._SYSTEMD_UNIT="metis-speech.service" OR jsonPayload._SYSTEMD_UNIT="caddy.service") AND (severity>=ERROR OR jsonPayload.MESSAGE=~"(?i)(traceback|exception|failed)" OR textPayload=~"(?i)(traceback|exception|failed)")'
if g logging metrics describe metis_service_errors >/dev/null 2>&1; then
  g logging metrics update metis_service_errors \
    --description="Metis systemd service failures and error logs" \
    --log-filter="$error_filter"
else
  g logging metrics create metis_service_errors \
    --description="Metis systemd service failures and error logs" \
    --log-filter="$error_filter"
fi

upsert_counter_metric() {
  local name="$1" description="$2" filter="$3"
  if g logging metrics describe "$name" >/dev/null 2>&1; then
    g logging metrics update "$name" --description="$description" --log-filter="$filter"
  else
    g logging metrics create "$name" --description="$description" --log-filter="$filter"
  fi
}

upsert_distribution_metric() {
  local name="$1" config="$2"
  if g logging metrics describe "$name" >/dev/null 2>&1; then
    g logging metrics update "$name" --config-from-file="$config"
  else
    g logging metrics create "$name" --config-from-file="$config"
  fi
}

caddy_access_filter='resource.type="gce_instance" AND jsonPayload._SYSTEMD_UNIT="caddy.service" AND jsonPayload.MESSAGE=~"\"logger\":\"http\\.log\\.access\\.[^\"]+\""'
upsert_counter_metric metis_caddy_requests \
  "All Caddy requests handled by the Metis public edge" "$caddy_access_filter"
upsert_counter_metric metis_caddy_5xx \
  "Caddy requests returning HTTP 5xx from the Metis public edge" \
  "$caddy_access_filter AND jsonPayload.MESSAGE=~\"\\\"status\\\":5[0-9][0-9](,|})\""
upsert_distribution_metric metis_caddy_request_latency_seconds \
  "$GCP_DIR/monitoring/caddy-latency-metric.json"
upsert_distribution_metric metis_outbox_backlog \
  "$GCP_DIR/monitoring/outbox-backlog-metric.json"
upsert_distribution_metric metis_outbox_oldest_age_seconds \
  "$GCP_DIR/monitoring/outbox-age-metric.json"

# Optional: NOTIFY_EMAIL=you@example.com attaches an email channel to every policy.
# Channels have no GA gcloud command, so use the REST API directly.
channel=""
if [ -n "${NOTIFY_EMAIL:-}" ]; then
  api="https://monitoring.googleapis.com/v3/projects/$PROJECT/notificationChannels"
  auth="Authorization: Bearer $(gcloud auth print-access-token)"
  # List returns {"notificationChannels": [...]} (or {}); create returns the channel.
  json_name() { python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("notificationChannels") or [d])[0].get("name",""))'; }
  channel="$(curl -fsS -H "$auth" -G "$api" \
    --data-urlencode "filter=type=\"email\" AND labels.email_address=\"$NOTIFY_EMAIL\"" | json_name)"
  if [ -z "$channel" ]; then
    channel="$(curl -fsS -H "$auth" -H 'Content-Type: application/json' -X POST "$api" \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"type":"email","displayName":"Metis alerts ("+sys.argv[1]+")","labels":{"email_address":sys.argv[1]}}))' "$NOTIFY_EMAIL")" \
      | json_name)"
  fi
  [ -n "$channel" ] || { echo "could not create notification channel" >&2; exit 1; }
fi

ensure_policy() {
  local display_name="$1"; shift
  local name
  name="$(g monitoring policies list \
    --filter="displayName=\"$display_name\"" --format='value(name)' --limit=1)"
  if [ -z "$name" ]; then
    name="$(g monitoring policies create --display-name="$display_name" "$@" --format='value(name)')"
  fi
  if [ -n "$channel" ]; then
    g monitoring policies update "$name" --add-notification-channels="$channel" >/dev/null
  fi
}

upsert_policy_file() {
  local display_name="$1" config="$2"
  local name
  name="$(g monitoring policies list \
    --filter="displayName=\"$display_name\"" --format='value(name)' --limit=1)"
  if [ -z "$name" ]; then
    name="$(g monitoring policies create --policy-from-file="$config" --format='value(name)')"
  else
    g monitoring policies update "$name" --policy-from-file="$config" >/dev/null
  fi
  if [ -n "$channel" ]; then
    g monitoring policies update "$name" --set-notification-channels="$channel" >/dev/null
  fi
}

ensure_policy "Metis API unavailable" \
  --condition-display-name="HTTPS health check is failing" \
  --condition-filter="resource.type=\"uptime_url\" AND metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.label.\"check_id\"=\"$uptime_id\"" \
  --duration=120s --if='< 1' --trigger-count=1 --combiner=OR \
  --documentation="The public Metis /v1/health endpoint has failed from an uptime-check region."

ensure_policy "Metis VM CPU high" \
  --condition-display-name="CPU utilization above 85%" \
  --condition-filter="resource.type=\"gce_instance\" AND metric.type=\"compute.googleapis.com/instance/cpu/utilization\" AND resource.label.\"instance_id\"=\"$instance_id\"" \
  --duration=300s --if='> 0.85' --trigger-count=1 --combiner=OR \
  --documentation="Metis VM CPU has exceeded 85% for five minutes."

ensure_policy "Metis VM memory high" \
  --condition-display-name="Memory used above 85%" \
  --condition-filter="resource.type=\"gce_instance\" AND metric.type=\"agent.googleapis.com/memory/percent_used\" AND resource.label.\"instance_id\"=\"$instance_id\" AND metric.label.\"state\"=\"used\"" \
  --duration=300s --if='> 85' --trigger-count=1 --combiner=OR \
  --documentation="Metis VM memory use has exceeded 85% for five minutes."

ensure_policy "Metis VM disk high" \
  --condition-display-name="Disk used above 85%" \
  --condition-filter="resource.type=\"gce_instance\" AND metric.type=\"agent.googleapis.com/disk/percent_used\" AND resource.label.\"instance_id\"=\"$instance_id\" AND metric.label.\"state\"=\"used\"" \
  --duration=300s --if='> 85' --trigger-count=1 --combiner=OR \
  --documentation="Metis VM disk use has exceeded 85%. A full disk stops SQLite, logs and uploads."

ensure_policy "Metis service errors" \
  --condition-display-name="A Metis systemd service logged an error" \
  --condition-filter="resource.type=\"gce_instance\" AND metric.type=\"logging.googleapis.com/user/metis_service_errors\" AND resource.label.\"instance_id\"=\"$instance_id\"" \
  --duration=0s --if='> 0' --trigger-count=1 --combiner=OR \
  --documentation="Caddy or a Metis systemd unit logged a failure/error."

upsert_policy_file "Metis API latency high" \
  "$GCP_DIR/monitoring/policy-caddy-latency.json"
upsert_policy_file "Metis API 5xx rate high" \
  "$GCP_DIR/monitoring/policy-caddy-5xx.json"
upsert_policy_file "Metis worker backlog high" \
  "$GCP_DIR/monitoring/policy-outbox-backlog.json"

dashboard_name="$(g monitoring dashboards list \
  --filter='displayName="Metis Operations"' --format='value(name)' --limit=1)"
dashboard_template="$GCP_DIR/monitoring/dashboard.json"
dashboard_config="$(mktemp)"
trap 'rm -f "$dashboard_config"' EXIT
if [ -z "$dashboard_name" ]; then
  sed -e '/__DASHBOARD_ETAG__/d' \
    -e "s/__INSTANCE_ID__/$instance_id/g" -e "s/__UPTIME_CHECK_ID__/$uptime_id/g" \
    "$dashboard_template" >"$dashboard_config"
  g monitoring dashboards create --config-from-file="$dashboard_config"
else
  dashboard_etag="$(g monitoring dashboards describe "$dashboard_name" --format='value(etag)')"
  sed -e "s/__DASHBOARD_ETAG__/$dashboard_etag/g" \
    -e "s/__INSTANCE_ID__/$instance_id/g" -e "s/__UPTIME_CHECK_ID__/$uptime_id/g" \
    "$dashboard_template" >"$dashboard_config"
  g monitoring dashboards update "$dashboard_name" --config-from-file="$dashboard_config"
fi

cat <<EOF
Observability service account: $OBS_SA
Attach it to a stopped VM with:
  gcloud --project $PROJECT compute instances set-service-account $VM --zone $ZONE \
    --service-account=$OBS_SA --scopes=cloud-platform
EOF
