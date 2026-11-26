#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../../.."
renderer=ops/gcp/render-caddyfile.sh
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

HOST=api.example.test "$renderer" >"$tmp/default.caddy"
grep -Fq 'reverse_proxy 127.0.0.1:8000 {' "$tmp/default.caddy"
grep -Fq 'reverse_proxy 127.0.0.1:8001 {' "$tmp/default.caddy"
grep -Fq 'handle /v1/changes {' "$tmp/default.caddy"
grep -Fq 'log {' "$tmp/default.caddy"
grep -Fq 'duration_format seconds' "$tmp/default.caddy"
if grep -Eq 'fail_duration|health_uri|max_fails|unhealthy_status' "$tmp/default.caddy"; then
  echo 'single backend must never be quarantined' >&2
  exit 1
fi

HOST=api.example.test \
  METIS_API_UPSTREAMS='10.10.0.2:8000,10.10.0.3:8000 app.internal:9000' \
  "$renderer" >"$tmp/multiple.caddy"
grep -Fq 'reverse_proxy 10.10.0.2:8000 10.10.0.3:8000 app.internal:9000 {' "$tmp/multiple.caddy"

grep -Fq 'health_uri /v1/health' "$tmp/multiple.caddy"
grep -Fq 'fail_duration 30s' "$tmp/multiple.caddy"
grep -Fq 'lb_policy least_conn' "$tmp/multiple.caddy"

if HOST=api.example.test METIS_CHANGES_UPSTREAMS='host:99999' "$renderer" >"$tmp/invalid" 2>&1; then
  echo 'renderer accepted an invalid changes port' >&2
  exit 1
fi

if HOST=api.example.test METIS_API_UPSTREAMS='127.0.0.1:0' "$renderer" >"$tmp/invalid" 2>&1; then
  echo 'renderer accepted an invalid port' >&2
  exit 1
fi

if HOST='api.example.test {' "$renderer" >"$tmp/injected" 2>&1; then
  echo 'renderer accepted an invalid host' >&2
  exit 1
fi

echo 'render-caddyfile tests passed'
