# GCP operations

Metis currently runs as a single `e2-medium` Compute Engine VM in
`us-central1-a`. Caddy is the public TLS edge; the API, worker, speech service,
and static server bind to loopback. Caddy can balance across additional private
API nodes through `METIS_API_UPSTREAMS`, but one VM is not high availability.

## Deploy

All deploys ship committed `HEAD`, not uncommitted workspace files:

```bash
PROJECT=metis-dev-856337 ops/gcp/deploy.sh
```

The deploy installs the Ops Agent, validates Caddy before reload, restarts the
four Metis systemd services, and verifies `/v1/health`. The VM identity has only
`roles/logging.logWriter` and `roles/monitoring.metricWriter`.

## Power schedule

```bash
PROJECT=metis-dev-856337 ops/gcp/configure-schedule.sh
```

The idempotent script creates an OAuth-authenticated Cloud Scheduler service
account with a custom role limited to `compute.instances.get`, `.start`, and
`.stop`, then applies these `America/New_York` schedules:

| Job | Schedule | Effect |
| --- | --- | --- |
| `metis-stop-morning` | `0 4 * * 1-6` | stop at 04:00 Monday-Saturday |
| `metis-stop-sunday` | `0 0 * * 0` | stop at Sunday midnight |
| `metis-start-day` | `30 11 * * 1-6` | start at 11:30 Monday-Saturday |

The VM therefore remains off all Sunday and from 04:00 through 11:30 on the
other six days. `automaticRestart` is disabled. Compute stop uses a clean guest
OS shutdown, and the VM has an additional two-minute graceful-shutdown window.

## Monitoring

```bash
PROJECT=metis-dev-856337 ops/gcp/configure-monitoring.sh
```

This creates the `Metis API HTTPS` uptime check, `metis_service_errors`
log-based metric, CPU/memory/uptime/service-error alert policies, and the
`Metis Operations` dashboard. Add a notification channel in Cloud Monitoring
and attach it to the policies; policies without a channel create visible
incidents but send no email, SMS, Slack, or PagerDuty notification.

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
