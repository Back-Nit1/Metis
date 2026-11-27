# GCP operations

Metis currently runs as a single `e2-medium` Compute Engine VM in
`us-central1-a`. Caddy proxies `/v1/*` API traffic over loopback and serves
nothing else (no web app is hosted). Caddy can balance across additional private
API nodes through `METIS_API_UPSTREAMS`, but one VM is not high availability.
The ordinary API process admits at most 200 concurrent connections or tasks and keeps a
200-connection socket backlog. This is independent of the exact rolling limit
of 100 requests per client per 60 seconds; overload receives HTTP 503 rather
than growing the API queue without bound.

## Deploy

All deploys ship committed `HEAD`, not uncommitted workspace files:

```bash
PROJECT=metis-dev-856337 ops/gcp/deploy.sh
```

The deploy installs the Ops Agent, validates Caddy before reload, restarts the
five Metis systemd services, and verifies `/v1/health`. See
[hosted desktop cutover](hosted-desktop.md) for the public sign-in configuration.
The VM identity has log/metric writer roles and object-creator access scoped
to the backup bucket. VM deletion
protection is enabled and its boot disk does not auto-delete with the instance;
these guard against accidental deletion but are not substitutes for backups.

The API edge emits structured Caddy access logs. The queue observer reads
SQLite in read-only mode once per minute and emits only aggregate outbox counts
and the oldest-waiting age; it never logs job payloads or user data.

## Continuous availability

```bash
PROJECT=metis-dev-856337 ops/gcp/configure-schedule.sh
```

This migration deletes the old stop/start and alert-muting Scheduler jobs,
re-enables the API uptime alert, enables Compute automatic restart, and starts
the VM if it is stopped. Public deployments run continuously. Automatic restart
covers Compute host failures; it does not undo a deliberate operator stop.
Systemd restarts failed Metis processes after three seconds.

## Backups and recovery

Provision the private backup bucket and disk policy before deploying:

```bash
PROJECT=metis-dev-856337 ops/gcp/configure-backups.sh
PROJECT=metis-dev-856337 ops/gcp/deploy.sh
```

The VM has object-creator permission only on this bucket. It cannot read or
delete old backups. Public access prevention and uniform bucket permissions are
enforced. Keep access to backup readers restricted: archives contain every
user's SQLite state, and disk snapshots also contain server credentials.

| Backup | Schedule | Retention | Scope |
| --- | --- | --- | --- |
| SQLite archive | 07:00 UTC daily, up to 5 min jitter; catches up after reboot | latest 7 local, 30 days in Cloud Storage plus 7-day soft-delete default | online SQLite backup API, all `*.sqlite` files, `install_id`, SHA-256 manifest |
| Disk snapshot | daily window starting 08:00 UTC (currently 4 hours) | 14 days while disk exists; kept if source disk is deleted | entire boot disk, including objects, recordings, configuration and SQLite WAL |

The logical backup performs SQLite integrity/foreign-key checks and a trial
restore before upload. Cloud Storage verifies the streamed upload checksum.
SQLite files are internally consistent; separate databases are not one atomic
snapshot. `metis.sqlite` is authoritative; `memory.sqlite` is a rebuildable
index. Disk snapshots are crash-consistent and include the WAL. Daily backup
can lose up to roughly 24 hours of new data; this is disaster recovery, not a
client offline cache or automatic rollback after an application crash.

Check and run a backup immediately:

```bash
sudo systemctl start metis-backup.service
sudo systemctl status metis-backup.service metis-backup.timer
sudo journalctl -u metis-backup -n 30 --no-pager
```

Restore drill on your laptop (never point this at a live directory):

```bash
# List archives and select an explicit timestamp.
gcloud --project metis-dev-856337 storage ls gs://metis-dev-856337-metis-backups/sqlite/
gcloud --project metis-dev-856337 storage cp gs://metis-dev-856337-metis-backups/sqlite/metis-TIMESTAMP.tar.gz /tmp/metis-restore.tar.gz
python3 ops/gcp/backup/sqlite-backup.py restore /tmp/metis-restore.tar.gz /tmp/metis-recovered
```

Python 3.12+ is required. Restore refuses nonempty destinations, invalid
checksums, corrupt databases, links, and unsafe archive paths. It validates all
files before publishing them. Archives do not include recordings: recover
`objects/` from the corresponding disk snapshot for full historical sources.
An older snapshot may lack objects referenced by a newer SQLite archive; use
one full disk snapshot for a coherent full-disk recovery point. Rebuild the
memory index if mixing logical backup dates.

For actual recovery, stop `metis-api`, `metis-changes`, `metis-worker`,
`metis-timers`, `metis-speech`, and `metis-backup.timer`; ensure no backup is
running. Save the old data directory before replacing any files. Restore into
a fresh directory, then replace SQLite files and `install_id` while **all
writers are stopped**, removing old `-wal`/`-shm` sidecars. Restore matching
objects if needed, set owner `metis:metis`, and restart the services/timer.
Do not replay old production reminders against real transports during a drill.
The CLI deliberately has no live-overwrite switch.

For whole-disk recovery, create a new disk from a selected snapshot, attach it
to an isolated recovery VM, and check SQLite integrity and objects before
cutover. Do not boot a recovered image with public traffic or reminder delivery
enabled until the old server is stopped; two independent copies would dispatch
old work. Restore testing here covers the logical archive, not GCE disk boot.

## Shared speech, timers and memory

`metis-speech` is the only Whisper owner (`small.en`, CPU int8, two threads).
Flow dictation and recording uploads use its authenticated loopback endpoint.
One jobs worker waits for that shared service rather than loading its own
model. Busy speech work retries through existing bounded outbox handling.
`metis-timers --mode timers --processes 1 --interval 2` has its own SQLite
connection and claims only schedule/cancellation/delivery events, including
past-due reminders. It builds no transcriber, embedder, or inference client.
`metis-worker --mode jobs` excludes those events, so a long recording cannot
hold reminder timers behind its decoder call.

Each service has `MemoryHigh` for reclaim pressure and `MemoryMax` for a hard
resident limit. Speech gets 1536 MiB maximum, jobs 384 MiB, API 512 MiB,
long-polls 256 MiB, timers 160 MiB, and Caddy 192 MiB. Backups have
256 MiB. The host has a persistent 2 GiB swap file with bounded per-service
swap use. These are starting budgets, not measured peak guarantees: inspect
`systemctl show <unit> -p MemoryCurrent -p MemoryPeak -p MemorySwapCurrent`
and adjust if real long recordings approach a cap. Swap absorbs spikes;
exceeding RAM plus allowed swap can still cause OOM termination.

Caddy routes `/v1/changes` to port 8001 (64 concurrent connections/backlog)
and other API traffic to 8000 (200 concurrent connections/backlog). Both use
the same state/authentication. Single-server routes never quarantine their
only upstream after errors. With multiple configured upstreams, active/passive
health checks and least-connections balancing remain enabled. Configure
`METIS_CHANGES_UPSTREAMS` separately when adding nodes.

Reminder labels turn from scheduled to due at the trigger time and overdue
after one minute, without requiring a scheduler write. Notifications dispatched
at least one minute late include elapsed minutes and the original scheduled
time. Quiet-hour deferral and snoozing use the effective revised trigger time.

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
