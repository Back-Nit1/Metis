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
branch="$(git symbolic-ref --short -q HEAD || true)"
if [ "$branch" != "main" ]; then
  echo "refusing to deploy: on '${branch:-detached HEAD}', not main. Merge to main first." >&2
  exit 1
fi
PROJECT="${PROJECT:?set PROJECT to the GCP project id}"
IP="$(gcloud --project "$PROJECT" compute addresses describe metis --region "${REGION:-us-central1}" --format='value(address)')"
echo "METIS_API_ORIGIN must be https://${IP//./-}.sslip.io"
if [ "${METIS_LEGACY_SINGLE_USER:-false}" = true ]; then
  export VITE_METIS_API="" VITE_METIS_LOGIN=password
else
  : "${VITE_METIS_FIREBASE_API_KEY:?set the public Identity Platform web API key}"
  : "${VITE_METIS_FIREBASE_PROJECT_ID:?set the Identity Platform project ID}"
  export VITE_METIS_FIREBASE_API_KEY VITE_METIS_FIREBASE_PROJECT_ID
  export VITE_METIS_API="https://${IP//./-}.sslip.io" VITE_METIS_LOGIN=firebase
fi

pnpm --filter @metis/web build
out="$(mktemp -d)"; trap 'rm -rf "$out"' EXIT
cp -R apps/web/dist/. "$out/" && cp ops/vercel/vercel.json "$out/" && cp -R ops/vercel/api "$out/api"
if [ "${METIS_LEGACY_SINGLE_USER:-false}" != true ]; then
  # Hosted clients use per-user tokens directly against GCP. Do not deploy the
  # legacy proxy/login endpoints that attach one shared VM identity.
  rm -r "$out/api"
  python3 - "$out/vercel.json" "$VITE_METIS_API" <<'PY'
import json, pathlib, sys
from urllib.parse import urlsplit
path, origin = pathlib.Path(sys.argv[1]), sys.argv[2]
parts = urlsplit(origin)
if parts.scheme != 'https' or not parts.hostname or any(c.isspace() for c in origin):
    raise SystemExit('invalid API origin')
config = json.loads(path.read_text())
config.pop('rewrites', None)
for group in config['headers']:
    for header in group['headers']:
        if header['key'] == 'Content-Security-Policy':
            header['value'] = header['value'].replace("connect-src 'self'", (
                "connect-src 'self' " + origin +
                " https://identitytoolkit.googleapis.com https://securetoken.googleapis.com"
            ))
path.write_text(json.dumps(config, indent=2) + '\n')
PY
fi
cd "$out"
vercel link --yes --project "${VERCEL_PROJECT:-metis}" >/dev/null
rm -f .env.local  # `vercel link` writes an OIDC token here; it must not be deployed
vercel deploy --prod --yes
