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

printf '%s {\n' "$HOST"
printf '\t@docs path /docs* /redoc* /openapi.json\n'
printf '\trespond @docs 404\n'
printf '\treverse_proxy'
printf ' %s' "${upstreams[@]}"
cat <<CADDY
 {
		# Active checks remove an unhealthy API before user traffic reaches it.
		health_uri /v1/health
		health_headers {
			Host $HOST
		}
		health_interval 10s
		health_timeout 2s
		health_status 200

		# Passive checks catch failures that occur between active probes.
		fail_duration 30s
		max_fails 2
		unhealthy_status 5xx

		lb_policy least_conn
		lb_try_duration 5s
		lb_try_interval 250ms
	}
}
CADDY
