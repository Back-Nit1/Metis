# GCP operations

Metis currently runs as a single `e2-medium` Compute Engine VM in
`us-central1-a`. Caddy is the public TLS edge; the API, worker, speech service,
and static server bind to loopback. Caddy can balance across additional private
API nodes through `METIS_API_UPSTREAMS`, but one VM is not high availability.
The API process admits at most 200 concurrent connections or tasks and keeps a
200-connection socket backlog. This is independent of the exact rolling limit
of 100 requests per client per 60 seconds; overload receives HTTP 503 rather
than growing the API queue without bound.

## Deploy

All deploys ship committed `HEAD`, not uncommitted workspace files:

```bash
PROJECT=metis-dev-856337 ops/gcp/deploy.sh
```

The deploy installs the Ops Agent, validates Caddy before reload, restarts the
four Metis systemd services, and verifies `/v1/health`. The VM identity has only
`roles/logging.logWriter` and `roles/monitoring.metricWriter`.

The API edge emits structured Caddy access logs. The queue observer reads
SQLite in read-only mode once per minute and emits only aggregate outbox counts
and the oldest-ready age; it never logs job payloads or user data.

## Power schedule

```bash
PROJECT=metis-dev-856337 ops/gcp/configure-schedule.sh
```

The idempotent script creates an OAuth-authenticated Cloud Scheduler service
account with a custom role limited to `compute.instances.get`, `.start`, and
`.stop`, then applies these `America/New_York` schedules:

| Job | Schedule | Effect |
| --- | --- | --- |
| `metis-stop-morning` | `0 4 * * *` | stop at 04:00 every day |
| `metis-start-day` | `30 11 * * *` | start at 11:30 every day |

The VM therefore remains off from 04:00 through 11:30 every day, including
Sunday. `automaticRestart` is disabled. Compute stop uses a clean guest OS
shutdown, and the VM has an additional two-minute graceful-shutdown window.

## Monitoring

```bash
PROJECT=metis-dev-856337 ops/gcp/configure-monitoring.sh
```

This creates the `Metis API HTTPS` uptime check; Caddy request, 5xx, latency,
service-error and outbox-backlog metrics; CPU/memory/uptime/error/latency/5xx/
backlog alert policies; and the `Metis Operations` dashboard. Set
`NOTIFY_EMAIL=you@example.com` to create (or reuse) an email notification
channel and attach it to every policy. Without it, policies create visible
incidents but send no email, SMS, Slack, or PagerDuty notification.

## Billing budget

```bash
NOTIFY_EMAIL=you@example.com PROJECT=metis-dev-856337 \
  BUDGET_AMOUNT_USD=25 ops/gcp/configure-budget.sh
```

The monthly budget is scoped to this project and warns at 50%, 80%, and 100%
of actual spend plus 100% of forecasted spend. Billing-account administrators
and users remain default recipients; `NOTIFY_EMAIL` adds an explicit recipient.
A GCP budget is an alert, not a hard spending cap.

- [VM details](https://console.cloud.google.com/compute/instancesDetail/zones/us-central1-a/instances/metis?project=metis-dev-856337)
- [Monitoring dashboards](https://console.cloud.google.com/monitoring/dashboards?project=metis-dev-856337)
- [Uptime checks](https://console.cloud.google.com/monitoring/uptime?project=metis-dev-856337)
- [Alert policies](https://console.cloud.google.com/monitoring/alerting/policies?project=metis-dev-856337)
- [Logs Explorer](https://console.cloud.google.com/logs/query?project=metis-dev-856337)
- [Cloud Scheduler](https://console.cloud.google.com/cloudscheduler?project=metis-dev-856337)

For a public/open-source deployment, keep Cloud Monitoring as the hosted GCP
default. Grafana plus Prometheus/OpenTelemetry is a useful portable option once
there are multiple nodes; do not host the only Grafana instance on the same VM
it is meant to report as down.
