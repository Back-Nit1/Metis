# Backup and reliability verification — 2026-11-26

Applied to the existing `metis-dev-856337` / `metis` VM in `us-central1-a`.
Runtime deployed from main commit `01f32a1977bcef6350c124d8688f6a22ee07bff2`.
The late-reminder UI was published to `https://metis-delta.vercel.app`.
No production data was overwritten or reminder transports replayed.

## Actual cloud round trip and local restore

1. Ran `metis-backup.service` on the live VM. Systemd returned `Result=success`,
   `ExecMainStatus=0`; the backup emitted `backup_verified`, `cloud_uploaded=true`.
2. Uploaded object:
   `gs://metis-dev-856337-metis-backups/sqlite/metis-20261126T221149.311297Z.tar.gz`.
   The service verified SQLite integrity, a temporary restore, and the cloud
   object's upload MD5 before reporting success.
3. Downloaded that exact object with `gcloud storage cp` to a private temporary
   local archive and ran the repository's `sqlite-backup.py restore` command
   into `/private/tmp/metis-cloud-recovered-20261126`.
4. The manifest SHA-256 and SQLite integrity checks passed. Restored:
   `metis.sqlite` (10 tables, 37 records), `memory.sqlite` (15 tables), and the
   installation identity. No user record contents or identity values are in
   this report. Restored files have mode 0600; the directory has mode 0700.

Additional automated tests restored committed data from a live WAL while an
uncommitted write remained open; the uncommitted write was absent from the
restored copy. Corrupt checksums and traversal archives were rejected before
any restored database was published. Nonempty destinations were refused.

The logical archive excludes recordings. Full-disk recovery requires the disk
snapshot and its object files. This drill did **not** boot a VM from a snapshot
or replace the running production database.

## Cloud configuration checked

- Automatic restart: `True`.
- Cloud Scheduler: the former daily stop/start and alert-muting jobs were
  deleted; the final regional job listing contained no jobs.
- Daily disk policy `metis-daily`: `daysInCycle=1`, starts at 08:00 UTC,
  provider window `PT14400S` (four hours), 14-day maximum retention while the
  disk exists, `KEEP_AUTO_SNAPSHOTS` if the source disk is deleted.
- Initial snapshot `metis-reliability-20261126`: `READY`. This manual recovery
  point has no automatic expiry; review/delete it after 14 days when daily
  snapshots have been verified.
- Private bucket: uniform access and public access prevention enabled;
  30-day lifecycle deletion, default seven-day soft-delete period. VM gets
  object-creator access scoped to this bucket.
- Backup timer enabled; next observed run 2026-11-27 at 07:04:58 UTC.

## Runtime checks

All API, long-poll, jobs, timers and speech services were active with zero
restarts after rollout. The service commands confirmed:

- API port 8000: concurrency/backlog 200, resident maximum 512 MiB.
- Long-polls port 8001: concurrency/backlog 64, resident maximum 256 MiB.
- Jobs: `--mode jobs --processes 1`, resident maximum 384 MiB.
- Timers: `--mode timers --processes 1 --interval 2`, maximum 160 MiB.
- Speech: explicit `faster_whisper` override, two CPU threads, maximum 1536 MiB.
  Jobs and API use the remote client; Flow and uploads share this decoder.
- Swap: `/var/swap/metis.swap`, 2 GiB, active, no swap in use at the check.

A five-second read-only load check issued 70 simultaneous change-feed requests:
63 returned 200 and seven returned the expected 503 concurrency rejection.
During saturation, three authenticated ordinary API reads returned 200, their
maximum wall time was 0.015 seconds, and health returned 200. The live Caddyfile
routed `/v1/changes` to 8001 and other requests to 8000, with no passive
quarantine settings. Public API health and the hosted UI responded successfully.

Timer tests also held a transcription handler blocked while a separate timer
connection dispatched due work; timer claim tests bypassed 30 older capture
jobs. Reminder notification tests checked a 7.5-hour delay is labeled as
450 minutes late; frontend tests checked due/overdue and snooze/terminal states.

## Speech measurement

See `services/speech/reports/2026-11-26-vm/` for sanitized measurements and
limitations. The requested `make speech-bench` ran on the VM with real audio,
remote decoder, two passes, and no dependency sync. Three unique recordings
contained 47.507 seconds per pass; 95.014 seconds total audio took 56.5116 seconds
HTTP wall time (RTF 0.5948). Accuracy was not inferred from the speed run.
A separate one-clip comparison to an existing user-corrected transcript measured
4% WER; it is a small correction pilot, not independent ground truth.

## Repository validation

Python regression suite passed with one existing skip (integration/cloud tests
excluded). All 134 web unit tests passed; web typecheck and production build,
production Python mypy, repository Ruff lint/format checks, Caddy renderer tests,
queue-observer check, and backup tests passed. Local socket tests required the
normal sandbox escalation; no external model service was used by unit tests.
