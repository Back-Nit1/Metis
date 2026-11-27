#!/usr/bin/env bash
# Render the public Caddy edge configuration.
#
# METIS_API_UPSTREAMS accepts a comma- or whitespace-separated list of
# host:port endpoints. Keep it to private/internal endpoints; Caddy is the
# only process that should listen on a public interface.
set -euo pipefail

: "${HOST:?set HOST to the public API hostname}"

if [[ ! "$HOST" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]]; then
  echo "invalid HOST: expected a hostname with an optional port" >&2
  exit 2
fi

raw_upstreams="${METIS_API_UPSTREAMS:-127.0.0.1:8000}"
raw_upstreams="${raw_upstreams//,/ }"
read -r -a upstreams <<<"$raw_upstreams"

if [ "${#upstreams[@]}" -eq 0 ]; then
  echo "METIS_API_UPSTREAMS must contain at least one host:port endpoint" >&2
  exit 2
fi

for upstream in "${upstreams[@]}"; do
  if [[ ! "$upstream" =~ ^[A-Za-z0-9.-]+:[0-9]{1,5}$ ]]; then
    echo "invalid API upstream '$upstream': expected host:port" >&2
    exit 2
  fi

  port="${upstream##*:}"
  if ((port < 1 || port > 65535)); then
    echo "invalid API upstream '$upstream': port must be 1-65535" >&2
    exit 2
  fi
done

# Change feeds have a separate listener and connection allowance.
raw_changes="${METIS_CHANGES_UPSTREAMS:-127.0.0.1:8001}"
raw_changes="${raw_changes//,/ }"
read -r -a changes_upstreams <<<"$raw_changes"
for upstream in "${changes_upstreams[@]}"; do
  if [[ ! "$upstream" =~ ^[A-Za-z0-9.-]+:[0-9]{1,5}$ ]] ||
     (( ${upstream##*:} < 1 || ${upstream##*:} > 65535 )); then
    echo "invalid changes upstream: expected host:port" >&2
    exit 2
  fi
done
[ "${#changes_upstreams[@]}" -gt 0 ] || exit 2

proxy() {
  printf '\t\treverse_proxy'
  printf ' %s' "$@"
  printf ' {\n'
  # Ejecting the sole backend cannot improve availability. Only balance and
  # quarantine failures when there is another backend to receive traffic.
  if [ "$#" -gt 1 ]; then
    cat <<CADDY
            health_uri /v1/health
            health_headers {
                Host $HOST
            }
            health_interval 10s
            health_timeout 2s
            health_status 200
            fail_duration 30s
            max_fails 2
            unhealthy_status 5xx
            lb_policy least_conn
            lb_try_duration 5s
            lb_try_interval 250ms
CADDY
  fi
  printf '\t\t}\n'
}
printf '%s {\n' "$HOST"
cat <<'CADDY'
    log {
        output stdout
        format json {
            duration_format seconds
        }
    }
    @docs path /docs* /redoc* /openapi.json
    respond @docs 404
    handle /v1/changes {
CADDY
proxy "${changes_upstreams[@]}"
printf '\t}\n\t@api path /v1 /v1/*\n\thandle @api {\n'
proxy "${upstreams[@]}"
cat <<'CADDY'
    }
    handle {
        root * /opt/metis/apps/web/dist
        header {
            Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com; worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            X-Content-Type-Options nosniff
            Referrer-Policy no-referrer
            Permissions-Policy "microphone=(self), camera=(), geolocation=()"
        }
        @revalidate path / /index.html /sw.js
        header @revalidate Cache-Control "no-cache"
        file_server
    }
}
CADDY
