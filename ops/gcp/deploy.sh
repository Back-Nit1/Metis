#!/usr/bin/env bash
# Provision (idempotent) and deploy Metis to one private GCE VM.
#
#   PROJECT=<gcp-project-id> ops/gcp/deploy.sh
#
# Caddy serves the browser build and routes /v1 requests to loopback services.
# SSH stays IAP-only. Configure per-user server mode with configure-hosted.sh.
#
# Ships committed HEAD (git archive), not the working tree.
set -euo pipefail
cd "$(dirname "$0")/../.."

# Only main is deployable: what runs on the VM must be what main says.
branch="$(git symbolic-ref --short -q HEAD || true)"
if [ "$branch" != "main" ]; then
  echo "refusing to deploy: on '${branch:-detached HEAD}', not main. Merge to main first." >&2
  exit 1
fi

PROJECT="${PROJECT:?set PROJECT to the GCP project id}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-$REGION-a}"
VM="${VM:-metis}"
OBS_SA_ID="${OBS_SA_ID:-metis-vm-observability}"
OBS_SA="$OBS_SA_ID@$PROJECT.iam.gserviceaccount.com"
# Comma- or whitespace-separated private API endpoints for Caddy. The default
# is the API process on this VM; add internal endpoints when API nodes are
# provisioned separately.
METIS_API_UPSTREAMS="${METIS_API_UPSTREAMS:-127.0.0.1:8000}"
METIS_CHANGES_UPSTREAMS="${METIS_CHANGES_UPSTREAMS:-127.0.0.1:8001}"
METIS_BACKUP_BUCKET="${METIS_BACKUP_BUCKET:-$PROJECT-metis-backups}"
# ponytail: e2-medium (2 shared vCPU, 4 GB) fits small.en int8 at concurrency 1;
# move to e2-standard-2+ if transcription latency or a larger model demands it.
MACHINE="${MACHINE:-e2-medium}"
BUILD_SHA="$(git rev-parse HEAD)"

g() { gcloud --project "$PROJECT" --quiet "$@"; }
ensure() { # ensure <describe args...> -- <create args...>
  local d=() ; while [ "$1" != "--" ]; do d+=("$1"); shift; done; shift
  g "${d[@]}" >/dev/null 2>&1 || g "$@"
}

g services enable compute.googleapis.com iap.googleapis.com logging.googleapis.com monitoring.googleapis.com

if ! g iam service-accounts describe "$OBS_SA" >/dev/null 2>&1; then
  g iam service-accounts create "$OBS_SA_ID" --display-name="Metis VM observability"
fi
for role in roles/logging.logWriter roles/monitoring.metricWriter; do
  for attempt in $(seq 1 10); do
    if g projects add-iam-policy-binding "$PROJECT" \
      --member="serviceAccount:$OBS_SA" --role="$role" --condition=None >/dev/null; then
      break
    fi
    if [ "$attempt" -eq 10 ]; then
      echo "observability service account IAM did not propagate after 10 attempts" >&2
      exit 1
    fi
    sleep 3
  done
done

ensure compute networks describe metis -- \
  compute networks create metis --subnet-mode=custom
ensure compute networks subnets describe metis --region "$REGION" -- \
  compute networks subnets create metis --network metis --region "$REGION" --range 10.10.0.0/24
ensure compute addresses describe metis --region "$REGION" -- \
  compute addresses create metis --region "$REGION"
# 80 is only for the ACME HTTP-01 challenge; Caddy redirects the rest to 443.
ensure compute firewall-rules describe metis-https -- \
  compute firewall-rules create metis-https --network metis \
  --allow tcp:80,tcp:443 --source-ranges 0.0.0.0/0 --target-tags metis-https
# Only Google's IAP TCP-forwarding range may reach SSH.
ensure compute firewall-rules describe metis-iap-ssh -- \
  compute firewall-rules create metis-iap-ssh --network metis \
  --allow tcp:22 --source-ranges 35.235.240.0/20
# Egress only (apt, PyPI, model weights at deploy time).
ensure compute routers describe metis --region "$REGION" -- \
  compute routers create metis --network metis --region "$REGION"
ensure compute routers nats describe metis --router metis --region "$REGION" -- \
  compute routers nats create metis --router metis --region "$REGION" \
  --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges
ensure compute instances describe "$VM" --zone "$ZONE" -- \
  compute instances create "$VM" --zone "$ZONE" --machine-type "$MACHINE" \
  --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
  --boot-disk-size 30GB --boot-disk-type pd-balanced \
  --subnet metis --no-address --service-account "$OBS_SA" --scopes cloud-platform \
  --tags metis-https --provisioning-model STANDARD --restart-on-failure \
  --graceful-shutdown --graceful-shutdown-max-duration=2m \
  --shielded-secure-boot --metadata enable-oslogin=TRUE

g compute instances set-scheduling "$VM" --zone "$ZONE" --restart-on-failure >/dev/null

# The SQLite databases and local object store live on the boot disk. Protect
# both the instance and disk from being erased by an accidental VM deletion;
# an explicit two-step teardown is safer than coupling data loss to one click.
g compute instances update "$VM" --zone "$ZONE" --deletion-protection >/dev/null
boot_disk="$(g compute instances describe "$VM" --zone "$ZONE" \
  --format='value(disks[0].source.basename())')"
g compute instances set-disk-auto-delete "$VM" --zone "$ZONE" \
  --disk "$boot_disk" --no-auto-delete >/dev/null

attached_sa="$(g compute instances describe "$VM" --zone "$ZONE" \
  --format='value(serviceAccounts[0].email)')"
if [ "$attached_sa" != "$OBS_SA" ]; then
  echo "VM service account is '$attached_sa', expected '$OBS_SA'." >&2
  echo "Run ops/gcp/configure-monitoring.sh, stop the VM, attach that account, and retry." >&2
  exit 1
fi

g compute instances add-tags "$VM" --zone "$ZONE" --tags metis-https >/dev/null
IP="$(g compute addresses describe metis --region "$REGION" --format='value(address)')"
if [ -z "$(g compute instances describe "$VM" --zone "$ZONE" --format='value(networkInterfaces[0].accessConfigs[0].natIP)')" ]; then
  g compute instances add-access-config "$VM" --zone "$ZONE" --access-config-name external-nat --address "$IP"
fi
HOST="${IP//./-}.sslip.io"
export VITE_METIS_API="" VITE_METIS_LOGIN=firebase
METIS_GOOGLE_SIGN_IN=false
if [ "${METIS_BROWSER_SESSIONS:-false}" = true ]; then
  export VITE_METIS_LOGIN=firebase-cookie
  if [ -n "${VITE_METIS_GOOGLE_CLIENT_ID:-}" ]; then
    export VITE_METIS_GOOGLE_CLIENT_ID
    METIS_GOOGLE_SIGN_IN=true
  fi
fi

# Validate the edge inputs before copying or changing anything on the VM.
HOST="$HOST" METIS_API_UPSTREAMS="$METIS_API_UPSTREAMS" METIS_CHANGES_UPSTREAMS="$METIS_CHANGES_UPSTREAMS" \
  METIS_GOOGLE_SIGN_IN="$METIS_GOOGLE_SIGN_IN" \
  ops/gcp/render-caddyfile.sh >/dev/null

# Build the web client from the same commit, in a clean export.
stage="$(mktemp -d)"; trap 'rm -rf "$stage" "$stage.tar.gz"' EXIT
git archive "$BUILD_SHA" | tar -x -C "$stage"
(cd "$stage" && pnpm install --frozen-lockfile --filter @metis/web... && pnpm --filter @metis/web build)
# macOS otherwise serializes com.apple.* metadata into pax headers that GNU
# tar on the VM cannot interpret, flooding deploy output with harmless noise.
COPYFILE_DISABLE=1 tar -czf "$stage.tar.gz" -C "$stage" \
  --exclude node_modules --exclude infra --exclude 'apps/desktop' .

ssh_ready() { g compute ssh "$VM" --zone "$ZONE" --tunnel-through-iap --command true 2>/dev/null; }
for _ in $(seq 20); do ssh_ready && break; sleep 6; done

g compute scp --zone "$ZONE" --tunnel-through-iap "$stage.tar.gz" "$VM:/tmp/metis.tar.gz"
g compute scp --zone "$ZONE" --tunnel-through-iap "$stage/ops/gcp/vm-setup.sh" "$VM:/tmp/vm-setup.sh"
rm -f "$stage.tar.gz"
printf -v VM_SETUP_COMMAND 'sudo env HOST=%q METIS_API_UPSTREAMS=%q METIS_CHANGES_UPSTREAMS=%q METIS_BACKUP_BUCKET=%q METIS_BUILD=%q METIS_GOOGLE_SIGN_IN=%q bash /tmp/vm-setup.sh' \
  "$HOST" "$METIS_API_UPSTREAMS" "$METIS_CHANGES_UPSTREAMS" "$METIS_BACKUP_BUCKET" "$BUILD_SHA" "$METIS_GOOGLE_SIGN_IN"
g compute ssh "$VM" --zone "$ZONE" --tunnel-through-iap --command "$VM_SETUP_COMMAND"

cat <<EOF

Deployed: https://$HOST (web and API)
Health: https://$HOST/v1/health
See ops/gcp/hosted-desktop.md for sign-in and cutover.
EOF
