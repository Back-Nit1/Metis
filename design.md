# Metis: core system design decisions

One page per decision: **what** we chose, **why**, and **where** it lives. The
full reasoning is in `docs/spec/*.md` (from `prompt (1).md`) and the ADRs in
`docs/architecture/`. This file is the summary; when they disagree, the ADRs win.

Last updated: 2026-11-21.

---

## 1. What the system is

A personal assistant you talk or type to. It turns a thought into a saved
**source** (recording or note), then **proposals** (suggested tasks, events,
reminders) that you accept or reject, then reminders and daily plans.

Transcription, extraction, state, scheduling, delivery and planning are
**separate components**. RAG (search over old notes) is one feature, not the
architecture.

## 2. Contracts first: one schema for everything

- **What:** Pydantic models in `packages/contracts/src/metis_contracts/` are
  the only hand-written definitions. A generator emits JSON Schema and
  TypeScript (`packages/contracts/generated/metis.ts`). CI fails if they drift.
- **Why:** three hand-kept copies (Python, TS, JSON) always drift, and drift
  between web and API is the bug we most want to prevent.
- **Rule:** only the coordinator edits contracts. Workers propose changes.
- ADR 0001.

## 3. Ownership comes from the server, never the request

- **What:** `user_id` comes only from the verified identity (Cognito JWT
  subject, desktop install subject, or local demo user). Request bodies, query
  strings and queue payloads never decide ownership. Every read and write is
  scoped by `user_id`, and object keys must sit under the owner's prefix
  (`owner_prefix`, `owns_key`).
- **Why:** it's the single rule that stops one user from reading another's data.
- **Where:** `services/api/src/metis_api/auth.py`, `deps.py`,
  `services/state/src/metis_state/object_store.py`.

## 4. Identity modes

| Mode | Who you are | Credential |
| --- | --- | --- |
| Cloud (prod) | Cognito JWT `sub` | Browser PKCE login; API verifies signature, issuer, expiry and client |
| Desktop | `desktop_<uuid>` per install | Per-launch random secret, loopback only |
| Single-user VM | the VM's install subject | One random access key, held only by the Vercel proxy |
| Local dev | `demo_authenticated_subject` | None. **Same-machine (loopback) callers only** |

- Prod refuses fixture identity, fixture inference and desktop mode at startup
  (`config.py`, `Settings.__post_init__`).
- The public Vercel UI is gated by a username/password login at the edge
  (`ops/vercel/api/`). The proxy adds the VM key only when the request carries
  a valid signed session cookie. The browser never sees the key.
- Cognito accounts replace the single login before anyone else uses it.

## 5. Honest states

- **What:** the UI never claims more than the system knows.
  - "Reminder scheduled" only after the scheduler confirms. Until then it's
    `pending_schedule`, or `schedule_failed` if that fails.
  - Uploaded is not transcribed. Delivered to push is not seen.
  - Fixture (fake) model or speech output is labelled as fixture, never shown
    as real. `/v1/health` reports every adapter's mode so the UI can tell.
- **Why:** a personal assistant that silently drops a reminder is worse than
  none.

## 6. Nothing is invented

- **What:** an unknown date, time or registration state stays `null`. Every
  derived fact carries `source_id` + `source_version` + `span_ref` back to the
  exact words. Estimates are labelled as estimates. Wanting to attend is not
  registering.
- **Why:** trust. You can always click through to why Metis thinks something.

## 7. Optimistic concurrency + idempotency

- **What:** every mutation sends `expected_revision`; a mismatch returns `409`.
  Mutations that create work send an `Idempotency-Key`. A repeat returns the
  stored first response, and a reused key with a different body is refused
  (request fingerprint).
- **Why:** phones retry, tabs race, and double-tapping must not create two
  tasks or two transcription jobs.
- **Where:** `deps.py` (`replay`/`remember`), state stores.

## 8. Transactional outbox for side effects

- **What:** an entity change and its side-effect request (schedule a reminder,
  index for search, export) commit in **one transaction**. A separate
  dispatcher performs the side effect later, with retries, leases and a
  dead-letter path. Each event carries a target revision, so stale events are
  dropped.
- **Why:** the database and EventBridge or push can't share one atomic
  transaction. The outbox means a committed change always has its follow-up,
  and a crash can't lose it.
- **Where:** `services/pipeline/src/metis_pipeline/dispatcher.py`.
- The due-reminder handler **re-checks everything** at fire time (owner,
  status, revision, quiet hours, cancellation) because a superseded schedule
  can still fire. Exactly-once delivery is **not** claimed.

## 9. Storage: one DynamoDB table, mirrored by SQLite

- **What:** cloud uses one on-demand table, `PK = USER#<sub>`, typed `SK`
  (`TASK#id`, `REV#…`, `OBX#…`, `IDEM#…`). Two indexes: due dates and the
  outbox queue. Local mode uses SQLite with the same semantics.
- **Why:** personal scale, no idle database cost, no table scans, and local dev
  needs no cloud account.
- **Rules:** secondary indexes only speed things up. The planner reads the
  authoritative rows with strong consistency. TTL is cleanup only, never a
  timer.
- ADR 0002.

## 10. Adapters (ports) so every mode runs the same code

- **What:** interfaces `StateStore`, `SourceStore`, `SpeechTranscriber`,
  `InferenceClient`, `MemoryStore`, `Embedder`, `Clock`, `ScheduleStore`,
  `NotificationTransport` (`metis_contracts/ports.py`). Each has
  fixture/local/cloud implementations.
- **Modes:** `METIS_MODE=local` (SQLite, files, fixture model and ASR),
  `hybrid` (local state, real models), `cloud` (DynamoDB, S3, Bedrock, ECS ASR,
  EventBridge).
- **Why:** local mode must work with zero accounts, and tests swap any adapter
  for a deterministic fixture.
- The same FastAPI app runs under uvicorn locally and Lambda in the cloud
  (Mangum), so there is no second HTTP path.

## 11. Capture pipeline

1. You start and stop recording deliberately. There is no always-on mic.
   Limits are 5 min and 25 MiB, checked in both the browser and the server.
2. Create capture, upload directly to storage (presigned S3, or
   `/v1/local-uploads` locally), then `complete`. The server checks owner, key
   prefix, existence and size. One transcription job per source version.
3. Transcript is saved **before** extraction. You can correct it; correction
   invalidates the old proposals.
4. Extraction: a small model, only the needed text, no tools. The note is
   **data, never instructions**. Output goes through schema validation plus at
   most one repair attempt. If it's still invalid, the proposal is unresolved
   and you get a retry or edit path.
5. Accepted items become tasks, events and reminders through the outbox.

## 12. Owned speech-to-text

- **What:** `faster-whisper` `small.en`, CPU int8, run by us. It's an ECS
  Fargate task in the cloud and a loopback server on the VM and desktop. Model
  weights are pinned by revision and fetched at deploy or install time, never
  per request.
- **Why:** no speech SaaS key, predictable cost, audio stays in our
  infrastructure.
- **Dictation ("Flow"):** a second voice mode. Hold a key, speak, and cleaned
  text is inserted into the focused app. Cleanup is deterministic, makes no
  model calls and saves nothing by default. ADR 0005.

## 13. Models and budgets

- **What:** extraction, planning and embedding model IDs are separate and
  configurable (defaults Nova Micro, Nova Lite, Titan v2). Prices are
  configuration, not facts.
- **Budget gate:** reserve the estimated cost atomically **before** a paid call,
  then settle the actual cost after. Daily and monthly caps per user.
  Deterministic reminders make **zero** model calls.
- **Why:** prevents a bug or an attacker from running up a bill
  (denial-of-wallet).
- **Where:** `services/pipeline/src/metis_pipeline/budget.py`.

## 14. Planning and retrieval

- Hard obligations (events, deadlines, tasks) come from **structured queries**,
  never from vector search. Vector search only adds context ("what did I try
  last time").
- A per-user `planning_epoch` changes with any relevant edit. Plans record it,
  and accepting a stale plan returns `409`.
- The model proposes blocks. **Deterministic code validates them** (overlaps,
  capacity, dependencies, deadlines). Work that doesn't fit is shown as
  overflow, not hidden.
- If a read limit truncates the input, the plan says it's incomplete.

## 15. Deletion and export

- Delete: immediate tombstone (you lose access at once), then a resumable
  async purge of objects, vectors and caches. Independently edited tasks are
  not silently deleted. Known gap: transcripts are not purged yet.
- Export: an authenticated job producing a zip, marked ready only after the
  write is verified. The download re-checks ownership and the checksum.

## 16. Request-edge security

- **Host guard:** stops DNS rebinding against the loopback API.
- **Loopback-only demo identity:** a LAN caller forging `Host: localhost` still
  gets `401`.
- **Body limit** enforced while bytes stream in (2 MiB JSON, 25 MiB upload).
- **Rate limits:** fixed window, all checked together, and a refused request
  counts nothing: per user 120/min and 2000/hour, whole process 600/min
  (`METIS_RATE_*`). Search has its own tighter ceiling. The API gateway
  throttle is the real limit in the cloud.
- **Login limits (Vercel edge):** 5/min and 20/hour per client, 30/min overall.
- **Correlation IDs** link API → queue → model → notification. Logs never
  contain audio, transcripts, prompts, tokens or URLs.
- **No credentials in the browser:** no AWS keys, Bedrock calls or VM key in
  `apps/web/`.
- **Outbound:** web push only to known push services. The local LLM and speech
  endpoints must be loopback.

## 17. Desktop shell

- Tauri v2 app loads the same web build (no second frontend). The HUD is a
  route of that build.
- A Python backend sidecar (PyInstaller) runs the API, worker and speech
  processes, all bound to `127.0.0.1:0` with per-launch secrets passed by
  environment only.
- Window permissions are minimal (no fs, shell or http plugins) and the CSP is
  strict.
- ADR 0004.

## 18. Deployment topology

| | Today (running) | Planned (written, never deployed) |
| --- | --- | --- |
| UI | Vercel static + edge proxy with login | S3 + CloudFront |
| API | One GCE VM, `METIS_MODE=local`, Caddy TLS on :443, loopback services | API Gateway (JWT authorizer) + Lambda |
| State | SQLite on the VM | DynamoDB (KMS) |
| Speech | faster-whisper on the VM | ECS Fargate task via Step Functions |
| Models | fixture (fake) extraction | Bedrock |
| Timers | local worker loop | EventBridge Scheduler |
| Infra | `ops/gcp/*.sh`, `ops/vercel/*` | AWS CDK, 7 stacks (`infra/`), synth passes |

## 19. Development process decisions

- Parallel agents, each in its own worktree and owned paths
  (`docs/spec/development.md`, `.claude/agents/`). Explicit `git add`, never
  `-A`.
- `mypy --strict` on production code, not tests (ADR 0003).
- `docs/progress.md` is the factual ledger: what was verified, with what
  command, and what is still unrun.

## Open decisions

- Live site: keep fixture extraction, run a local LLM on the VM, or move to
  Bedrock?
- Auto-scheduling "clear" reminder proposals vs always asking (conflicts a bit
  with §11 step 5's review-first flow).
- AWS budget: raise to ~$40/mo or drop interface endpoints.
- When to replace the single login with Cognito accounts.

See `notes.md` for the to-do list.
