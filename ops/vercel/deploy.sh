#!/usr/bin/env bash
# Publish the static UI to Vercel, pointed at the VM's public API host.
#   PROJECT=<gcp-project-id> ops/vercel/deploy.sh
# The UI calls /v1/* on its own origin; ops/vercel/api/proxy.js adds the access
# key from the Vercel env vars METIS_API_ORIGIN and METIS_ACCESS_KEY (set once
# with `vercel env add ... production`), so the bundle holds no secret.
set -euo pipefail
cd "$(dirname "$0")/../.."
PROJECT="${PROJECT:?set PROJECT to the GCP project id}"
IP="$(gcloud --project "$PROJECT" compute addresses describe metis --region "${REGION:-us-central1}" --format='value(address)')"
echo "METIS_API_ORIGIN must be https://${IP//./-}.sslip.io"
export VITE_METIS_API=""

pnpm --filter @metis/web build
out="$(mktemp -d)"; trap 'rm -rf "$out"' EXIT
cp -R apps/web/dist/. "$out/" && cp ops/vercel/vercel.json "$out/" && cp -R ops/vercel/api "$out/api"
cd "$out"
vercel link --yes --project "${VERCEL_PROJECT:-metis}" >/dev/null
rm -f .env.local  # `vercel link` writes an OIDC token here; it must not be deployed
vercel deploy --prod --yes
