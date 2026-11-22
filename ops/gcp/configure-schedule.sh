#!/usr/bin/env bash
# Idempotently configure Metis VM power hours.
#
# The VM is off 04:00-11:30 every day, in New York local time. Cloud Scheduler
# calls the Compute API directly with OAuth; no always-on function is needed
# merely to turn the VM on and off.
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT to the GCP project id}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-$REGION-a}"
VM="${VM:-metis}"
TIME_ZONE="${TIME_ZONE:-America/New_York}"
POWER_SA_ID="${POWER_SA_ID:-metis-vm-power-scheduler}"
ROLE_ID="${ROLE_ID:-metisVmPowerScheduler}"
POWER_SA="$POWER_SA_ID@$PROJECT.iam.gserviceaccount.com"

g() { gcloud --quiet --project "$PROJECT" "$@"; }

g services enable cloudscheduler.googleapis.com compute.googleapis.com

if ! g iam service-accounts describe "$POWER_SA" >/dev/null 2>&1; then
  g iam service-accounts create "$POWER_SA_ID" \
    --display-name="Metis VM power scheduler"
fi

permissions="compute.instances.get,compute.instances.start,compute.instances.stop"
if g iam roles describe "$ROLE_ID" >/dev/null 2>&1; then
  g iam roles update "$ROLE_ID" \
    --title="Metis VM power scheduler" \
    --description="Start and stop the Metis VM on its cost-control schedule" \
    --permissions="$permissions" \
    --stage=GA
else
  g iam roles create "$ROLE_ID" \
    --title="Metis VM power scheduler" \
    --description="Start and stop the Metis VM on its cost-control schedule" \
    --permissions="$permissions" \
    --stage=GA
fi

for attempt in $(seq 1 10); do
  if g projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$POWER_SA" \
    --role="projects/$PROJECT/roles/$ROLE_ID" \
    --condition=None >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 10 ]; then
    echo "service account IAM binding did not propagate after 10 attempts" >&2
    exit 1
  fi
  sleep 3
done

upsert_job() {
  local name="$1" schedule="$2" action="$3" description="$4"
  local uri="https://compute.googleapis.com/compute/v1/projects/$PROJECT/zones/$ZONE/instances/$VM/$action"
  local verb=create
  if g scheduler jobs describe "$name" --location="$REGION" >/dev/null 2>&1; then
    verb=update
  fi
  g scheduler jobs "$verb" http "$name" \
    --location="$REGION" \
    --description="$description" \
    --schedule="$schedule" \
    --time-zone="$TIME_ZONE" \
    --uri="$uri" \
    --http-method=POST \
    --oauth-service-account-email="$POWER_SA" \
    --oauth-token-scope=https://www.googleapis.com/auth/cloud-platform \
    --attempt-deadline=30s \
    --max-retry-attempts=3 \
    --max-retry-duration=15m \
    --min-backoff=30s \
    --max-backoff=5m
}

upsert_job metis-stop-morning "0 4 * * *" stop \
  "Stop Metis at 04:00 every day (America/New_York)"
upsert_job metis-start-day "30 11 * * *" start \
  "Start Metis at 11:30 every day (America/New_York)"

# Removed policy: Sunday now follows the same morning-only shutdown window.
if g scheduler jobs describe metis-stop-sunday --location="$REGION" >/dev/null 2>&1; then
  g scheduler jobs delete metis-stop-sunday --location="$REGION"
fi

g scheduler jobs list --location="$REGION" \
  --filter='name~metis-(start|stop)' \
  --format='table(name.basename(),schedule,timeZone,state,httpTarget.uri)'
