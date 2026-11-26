#!/usr/bin/env bash
# Private logical backups + daily whole-disk snapshots. Creates paid storage.
set -euo pipefail
PROJECT="${PROJECT:?set PROJECT to the GCP project id}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-$REGION-a}"
VM="${VM:-metis}"
BUCKET="${METIS_BACKUP_BUCKET:-$PROJECT-metis-backups}"
POLICY="${SNAPSHOT_POLICY:-metis-daily}"
g() { gcloud --quiet --project "$PROJECT" "$@"; }

# Fail before changing anything if the instance/project cannot be read.
project_number="$(g projects describe "$PROJECT" --format='value(projectNumber)')"
boot_disk="$(g compute instances describe "$VM" --zone="$ZONE" --format='value(disks[0].source.basename())')"
identity="$(g compute instances describe "$VM" --zone="$ZONE" --format='value(serviceAccounts[0].email)')"
[ -n "$boot_disk" ] && [ -n "$identity" ]
if ! g storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
  g storage buckets create "gs://$BUCKET" --location="$REGION" \
    --uniform-bucket-level-access --public-access-prevention
fi
bucket_project="$(g storage buckets describe "gs://$BUCKET" --raw --format='value(projectNumber)')"
if [ "$bucket_project" != "$project_number" ]; then
  echo 'backup bucket belongs to another project; refusing to modify it' >&2
  exit 1
fi
lifecycle="$(mktemp)"
trap 'rm -f "$lifecycle"' EXIT
cat >"$lifecycle" <<'JSON'
{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}
JSON
g storage buckets update "gs://$BUCKET" --uniform-bucket-level-access \
  --public-access-prevention --lifecycle-file="$lifecycle"
g storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$identity" --role=roles/storage.objectCreator >/dev/null

verb=create
if g compute resource-policies describe "$POLICY" --region="$REGION" >/dev/null 2>&1; then
  verb=update
fi
# Compute chooses a start in its daily window beginning 08:00 UTC; these are crash-consistent
# whole-disk snapshots. The independent SQLite archive is application-consistent.
args=(compute resource-policies "$verb" snapshot-schedule "$POLICY"
  --region="$REGION" --daily-schedule --start-time=08:00
  --max-retention-days=14 --on-source-disk-delete=keep-auto-snapshots)
if [ "$verb" = create ]; then
  args+=(--storage-location="$REGION")
fi
g "${args[@]}"
attached="$(g compute disks describe "$boot_disk" --zone="$ZONE" --format='value(resourcePolicies)')"
if [[ "$attached" != *"/resourcePolicies/$POLICY"* ]]; then
  g compute disks add-resource-policies "$boot_disk" --zone="$ZONE" --resource-policies="$POLICY"
fi
printf 'Backup bucket: gs://%s (30 days + Cloud Storage soft-delete retention)\n' "$BUCKET"
printf 'Disk policy: %s (daily, 14 days; retained if the source disk is deleted)\n' "$POLICY"
printf 'Deploy main with METIS_BACKUP_BUCKET=%s to enable the SQLite upload timer.\n' "$BUCKET"
