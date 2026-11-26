#!/usr/bin/env bash
# Migrate the former dev power schedule to continuous public availability.
set -euo pipefail
PROJECT="${PROJECT:?set PROJECT to the GCP project id}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-$REGION-a}"
VM="${VM:-metis}"
g() { gcloud --quiet --project "$PROJECT" "$@"; }

# Listing must succeed: an auth/network error must not be mistaken for no jobs.
jobs="$(g scheduler jobs list --location="$REGION" --format='value(name.basename())')"
for name in metis-stop-morning metis-start-day metis-stop-sunday \
  metis-mute-planned-downtime metis-unmute-after-start; do
  if echo "$jobs" | grep -Fxq "$name"; then
    g scheduler jobs delete "$name" --location="$REGION"
  fi
done
policies="$(g monitoring policies list --filter='displayName="Metis API unavailable"' --format='value(name)')"
while IFS= read -r policy; do
  [ -z "$policy" ] || g monitoring policies update "$policy" --enabled
 done <<<"$policies"
g compute instances set-scheduling "$VM" --zone="$ZONE" --restart-on-failure
status="$(g compute instances describe "$VM" --zone="$ZONE" --format='value(status)')"
if [ "$status" = TERMINATED ]; then
  g compute instances start "$VM" --zone="$ZONE"
fi
