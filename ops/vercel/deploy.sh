#!/usr/bin/env bash
# Publish the static UI to Vercel, pointed at the VM's public API host.
#   PROJECT=<gcp-project-id> ops/vercel/deploy.sh
# The UI calls /v1/* on its own origin. ops/vercel/api/proxy.js forwards only
# requests with a valid username/password session (api/login.js) and only then
# adds the VM access key, so the bundle and the browser hold no secret.
# Vercel env vars (production, set once with `vercel env add NAME production`):
#   METIS_API_ORIGIN           https://<ip>.sslip.io (printed below)
#   METIS_ACCESS_KEY           the VM key (/etc/metis-api.env)
#   METIS_LOGIN_USER           your username
#   METIS_LOGIN_PASSWORD_HASH  node ops/vercel/hash-password.mjs  (reads the password on stdin)
#   METIS_SESSION_SECRET       openssl rand -base64 48   (rotate to sign everyone out)
set -euo pipefail
cd "$(dirname "$0")/../.."
PROJECT="${PROJECT:?set PROJECT to the GCP project id}"
IP="$(gcloud --project "$PROJECT" compute addresses describe metis --region "${REGION:-us-central1}" --format='value(address)')"
echo "METIS_API_ORIGIN must be https://${IP//./-}.sslip.io"
export VITE_METIS_API="" VITE_METIS_LOGIN=password

pnpm --filter @metis/web build
out="$(mktemp -d)"; trap 'rm -rf "$out"' EXIT
cp -R apps/web/dist/. "$out/" && cp ops/vercel/vercel.json "$out/" && cp -R ops/vercel/api "$out/api"
cd "$out"
vercel link --yes --project "${VERCEL_PROJECT:-metis}" >/dev/null
rm -f .env.local  # `vercel link` writes an OIDC token here; it must not be deployed
vercel deploy --prod --yes
