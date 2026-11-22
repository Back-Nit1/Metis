# Agent C — Backend gaps the UI keeps hitting

You are Agent C of four parallel workers. Work in your own git worktree branched
from `27a89a105a2297fad5a685d74a04b16dee07c89d` (branch `agent-c-backend`).
Read `CLAUDE.md`, `docs/spec/architecture.md`, and the "Missing API" notes in
`new.md` first.

## Context

A real-user walkthrough of https://metis-delta.vercel.app/ found product dead
ends caused by missing API:

- Projects screen: "This build cannot create one by hand."
- No way to reschedule a reminder.
- No way to attach a capture/source to a project.
- Settings: "Push registration is not available yet: the server does not
  publish the public key."
- `/v1/health` and `/v1/limits` are open (no auth).

## Owned paths (touch nothing else)

- `services/api/`, `services/state/`, `services/scheduler/`, `services/planner/`

`packages/contracts/` is coordinator-only. Contract-first workflow: for each
feature below, FIRST write the proposed schema/route change (JSON Schema
fragment, request/response examples, error codes) in your final message and in
a scratch file outside the repo, then ask the coordinator to land it. Implement
against existing contract v1 where possible; do not fork a second schema. While
waiting, work on items that need no contract change (5, 6).

## Tasks

1. Reminder reschedule: mutation with expected `revision` (409 on conflict) and
   `Idempotency-Key`; writes an outbox row in the same transaction; status is
   `pending_schedule` until the scheduler confirms, `schedule_failed` on
   failure. Never report "scheduled" without confirmed sync.
2. `POST /v1/projects` (manual create) and `PATCH /v1/projects/{id}` with
   revision checks.
3. Attach an existing capture/source to a project (revision-checked,
   idempotent, ownership from the authenticated subject).
4. Web push: expose the VAPID public key when configured; otherwise return a
   clear machine-readable "not configured" the UI can render.
5. Decide whether `/v1/health` and `/v1/limits` should be gated. Prefer: health
   stays open and minimal (no internals), limits requires auth. Implement and
   justify in the final message.
6. Audit every existing mutation route for: expected `revision` -> 409,
   `Idempotency-Key` where work is created, entity change + side-effect in one
   outbox transaction, `user_id` from the authenticated subject, S3 keys
   restricted to the owner's prefix. Add tests for every gap found.

## Rules

- Local mode (`METIS_MODE=local`, SQLite) must keep working with no cloud
  account. Do not run any AWS call.
- Stage explicit paths only; never `git add -A`. Commit at safe boundaries,
  label incomplete work `checkpoint:` in the body.

## Done when

`make test` and `make check` pass; each new route has tests for success, 409,
idempotent replay and wrong-owner rejection. Final message lists each new or
changed route with request/response examples (for Agent B), the contract
change requests (for the coordinator), and the audit findings.
