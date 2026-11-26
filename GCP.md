# Using GCP for Metis

How to deploy, watch, and debug the Metis server on Google Cloud. Script
details live in `ops/gcp/README.md`; this is the operator's guide.

## The setup

- **Project:** `metis-dev-856337`
- **VM:** `metis`, an `e2-medium` (2 shared vCPUs, 4 GB RAM) in `us-central1-a`
- **Services on the VM (systemd):** `caddy` (public TLS edge),
  `metis-api`, `metis-worker`, `metis-speech` (faster-whisper). The API,
  worker, and speech service bind to loopback; only Caddy is public.
- **Health endpoint:** `https://<ip-with-dashes>.sslip.io/v1/health`
- **One VM is not high availability.** If it dies, Metis is down.
- **`automaticRestart` is disabled.** If the VM itself crashes it stays off
  until the next scheduled start or a manual start. (systemd still restarts
  individual crashed services.)

## Console links

| What            | Link                                                                                                                  |
| --------------- | --------------------------------------------------------------------------------------------------------------------- |
| Dashboard       | https://console.cloud.google.com/monitoring/dashboards?project=metis-dev-856337                                       |
| Uptime checks   | https://console.cloud.google.com/monitoring/uptime?project=metis-dev-856337                                           |
| Alert policies  | https://console.cloud.google.com/monitoring/alerting/policies?project=metis-dev-856337                                |
| Logs Explorer   | https://console.cloud.google.com/logs/query?project=metis-dev-856337                                                  |
| VM details      | https://console.cloud.google.com/compute/instancesDetail/zones/us-central1-a/instances/metis?project=metis-dev-856337 |
| Cloud Scheduler | https://console.cloud.google.com/cloudscheduler?project=metis-dev-856337                                              |

## Common commands

```bash
# Deploy committed HEAD (not uncommitted files)
PROJECT=metis-dev-856337 ops/gcp/deploy.sh

# Create/refresh uptime check, alerts, dashboard (+ email alerts)
NOTIFY_EMAIL=you@example.com PROJECT=metis-dev-856337 ops/gcp/configure-monitoring.sh

# Create/refresh the power schedule
PROJECT=metis-dev-856337 ops/gcp/configure-schedule.sh

# Start / stop the VM by hand
gcloud compute instances start metis --zone us-central1-a --project metis-dev-856337
gcloud compute instances stop  metis --zone us-central1-a --project metis-dev-856337

# SSH in
gcloud compute ssh metis --zone us-central1-a --project metis-dev-856337
```

All scripts are idempotent: safe to re-run.

## Power schedule (cost control)

Times are `America/New_York`:

| Job                  | Schedule      | Effect |
| -------------------- | ------------- | ------ |
| `metis-stop-morning` | 04:00 Mon–Sat | stop   |
| `metis-stop-sunday`  | 00:00 Sun     | stop   |
| `metis-start-day`    | 11:30 Mon–Sat | start  |

The VM is **off all Sunday and 04:00–11:30 every other day**. Uptime failures
and empty charts in those windows are expected, not incidents.

## The dashboard: what to watch

The `Metis Operations` dashboard has five charts. Check them in this order.

### 1. HTTPS uptime check: is it up?

Hits `/v1/health` through Caddy every minute from several regions. This is
what a real user experiences.

- **Normal:** flat line at 1.0 (100% passing).
- **Partial dip:** some regions failing, likely a network blip.
- **Drop to 0:** server is down.
- Outside the scheduled off-hours, any drop is real.

### 2. VM memory percent used: the most likely killer

Whisper holds a model in RAM, and 4 GB is tight alongside the API, worker, and
Caddy.

- **Normal:** steady plateau (roughly 50–70%) that bumps during transcription
  and comes back down.
- **Bad:** a line that climbs and never returns (leak); a sudden cliff to
  near zero right before an uptime failure (the OOM killer shot a process).
- Read the shape, not the single number.
- Alert: more than 85% for 5 minutes.

### 3. Metis service errors: why it broke

Counts error/traceback/"failed" lines from the four systemd services.

- **Normal:** zero, maybe a rare spike.
- **Steady drip:** a bug hit on every request.
- **Spike right after a deploy:** the deploy broke something.
- When it spikes, go to Logs Explorer (below) for the actual traceback.

### 4. VM disk percent used: the silent killer

Logs, uploads, and SQLite grow quietly. A full disk takes everything down.

- **Normal:** slow, steady growth.
- **Bad:** approaching 85%+, or a sudden jump.
- Alert: more than 85% for 5 minutes.

### 5. VM CPU utilization: least urgent

e2-medium has burstable shared CPU. Transcription spikes it, which is fine.

- **Bad:** a long plateau above 85%, meaning a runaway loop or a speech
  backlog.
- Alert: more than 85% for 5 minutes.

### Reading charts together

| You see                                   | Probably                                               |
| ----------------------------------------- | ------------------------------------------------------ |
| Uptime fails, memory cliff just before    | OOM kill; whisper + 4 GB too tight                     |
| Uptime fails, errors spike                | Code crash; read the traceback                         |
| Uptime fails, all charts go flat or empty | VM stopped (schedule, preemption, manual)              |
| Uptime fine, errors climbing              | Partial breakage; health passes but features fail      |
| CPU pinned, memory fine                   | Runaway job or speech backlog                          |
| Disk climbing toward full                 | Logs or uploads filling up; clean up before it crashes |

## Alerts and notifications

Policies created by `configure-monitoring.sh`:

- Metis API unavailable (health check failing for 2 minutes)
- Metis VM CPU high
- Metis VM memory high
- Metis VM disk high
- Metis service errors

**A policy only notifies you if it has a notification channel.** Without one,
incidents show up in the console but nobody is told. Run
`configure-monitoring.sh` with `NOTIFY_EMAIL` set to attach an email channel to
every policy. To add Slack, SMS, or PagerDuty: Monitoring → Alerting → **Edit
notification channels**, then attach the channel to each policy.

Expect "API unavailable" alerts during the scheduled off-hours. Snooze them in
the console (Alerting → **Snooze**) if they get noisy.

## Reading logs

The Ops Agent ships every systemd journal line from the VM to Cloud Logging,
so logs are readable even while the VM is off. Default retention is 30 days.

Useful fields on each entry:

- `jsonPayload._SYSTEMD_UNIT`: which service (`metis-api.service`,
  `metis-worker.service`, `metis-speech.service`, `caddy.service`)
- `jsonPayload.MESSAGE`: the log text
- `severity`: DEFAULT, INFO, WARNING, ERROR, …
- `timestamp`: displayed in your local time

### Option 1: Logs Explorer (console)

Layout: query box (top), histogram (middle), entries (bottom).

- Type a query and press **Run query**.
- Drag across the histogram to zoom into a time range. This is the fastest
  way to find a crash.
- Click an entry to expand all its fields.
- The time range defaults to the last hour. Widen it (top right) if you see
  nothing.
- **Stream logs** shows live tail, handy during a deploy.
- **Save** a query you reuse; find it later under **Saved queries**.

Queries (each line is ANDed):

```
# One service
resource.type="gce_instance"
jsonPayload._SYSTEMD_UNIT="metis-api.service"
```

```
# Errors, any service
resource.type="gce_instance"
severity>=ERROR
```

```
# Python crashes
resource.type="gce_instance"
jsonPayload.MESSAGE=~"Traceback|Exception"
```

```
# Free-text search (any field)
resource.type="gce_instance"
"some-request-id-or-word"
```

```
# Service crashes, restarts, OOM kills
resource.type="gce_instance"
jsonPayload.MESSAGE=~"Out of memory|oom-kill|Main process exited|Started metis|Stopping"
```

**Tracebacks arrive as many separate entries** (one per line). Find the
`Traceback` line, open its menu, and choose **Show entries around this one** to
read it in order.

If `severity>=ERROR` comes back empty during a known failure, use the
`Traceback|Exception` text search instead.

### Option 2: gcloud from your laptop

Works while the VM is off. Same query language, with `AND` between terms:

```bash
gcloud logging read \
  'resource.type="gce_instance" AND jsonPayload._SYSTEMD_UNIT="metis-api.service" AND severity>=ERROR' \
  --project metis-dev-856337 --freshness=2h --limit=50 \
  --format='value(timestamp, jsonPayload.MESSAGE)'
```

### Option 3: SSH and journalctl

The VM must be running. This gives the raw journal with no delay, which is
best for live debugging:

```bash
gcloud compute ssh metis --zone us-central1-a --project metis-dev-856337

journalctl -u metis-api -n 100 --no-pager       # last 100 lines
journalctl -u metis-worker -f                   # follow live (Ctrl-C to quit)
journalctl -u metis-speech --since "30 min ago"
journalctl -p err -b                            # errors since boot, all units
systemctl status metis-api                      # running? last exit code?
dmesg -T | grep -i oom                          # kernel OOM kills
```

## When the server crashes: playbook

1. **Is this the schedule?** Sunday, or 04:00–11:30 on another day, means it's
   off on purpose.
2. **Dashboard:** note when the uptime check started failing and what memory,
   disk, and errors did just before.
3. **Logs Explorer:** set the time range to a few minutes before the failure
   and run `severity>=ERROR`. Expand the first error and use **Show entries
   around this one**.
4. **No errors at all?** Run the OOM/restart query. A silent death is usually
   the kernel killing whisper for memory.
5. **Logs stop entirely at that moment?** The VM itself went down. Check
   Compute Engine → VM → **Logs** / **Observability** for stop events. Start
   it manually if needed; it won't restart on its own.
6. **Services up but broken?** SSH in, run `systemctl status <service>` and
   `journalctl -u <service> -n 200`.
7. **After a fix:** redeploy with `ops/gcp/deploy.sh`. It verifies
   `/v1/health` before finishing.

## Not monitored yet

Worth adding, in order of value:

1. **Request latency and 5xx rate** from Caddy. Shows the server struggling
   before it falls over.
2. **Worker/outbox backlog.** Pending jobs piling up means late reminders even
   while health looks fine.
3. **Model spend vs budget.** Set a billing budget alert in GCP/AWS so Bedrock
   cost can't run away quietly.
