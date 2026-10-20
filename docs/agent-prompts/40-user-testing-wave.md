# Wave 4 — real-user testing findings and four parallel prompts

Source: manual walkthrough of https://metis-delta.vercel.app/ on 2026-10-19 plus a
code read. Run the four prompts concurrently; write paths are disjoint. Replace
`{{BASE_SHA}}` with the coordinator's verified base. Coordinator alone edits
`packages/contracts/`, `docs/`, manifests, lockfiles.

## What a real user hits (evidence)

1. **Extraction drops most of the note.** Typed "Dentist next Tuesday at 3.
   Register for the Boston half marathon by March 3rd. Remind me to call mom
   tomorrow at 6pm. Buy milk." Result: ONE item, "Register for the event", no
   deadline. Lost: dentist event, reminder, milk task, and the "Boston half
   marathon" name and its March 3rd deadline. Cause: prod runs
   `FixtureInferenceClient` (regex stub, `services/pipeline/.../inference.py`
   `_fixture_extraction`); the UI banner says FIXTURE MODE, so it is honest but
   the product looks broken.
2. **Capture page feels unfinished after send.** Typed-note flow shows a
   "Transcribing" step for text. Review card sits above the input; after save
   the note stays on screen with no "what now" (no link to Inbox/Tasks, no
   "capture another").
3. **Raw ids leak.** "from src_0a196c45…" on Inbox and Today. Meaningless.
4. **Jargon leaks.** "planning epoch 0/1", "Server state: Scheduling: not
   confirmed yet", "r1" revision on task rows, "extract-1", "fixture-inference".
5. **Routing.** Unknown hash (`#/notifications`) silently renders Today; route is
   `#/alerts`. Tab title is always "Metis" on every screen.
6. **Dead ends.** Projects: "This build cannot create one by hand." Search is a
   blank page with no examples. Push: "server does not publish the public key".
   Missing API: reminder reschedule, project PATCH, attach capture to project.
7. **A11y.** Sidebar nav buttons expose no accessible name in the accessibility
   tree (several `button` with no label). Needs verification and fix.
8. **Mobile unverified.** One `@media (max-width: 767px)` block in
   `apps/web/src/styles.css`; never exercised in a real narrow viewport.
9. **Today** shows "Free time is unknown" warning to every new user before they
   have done anything; first run is cold with an empty plan form.

---

## Prompt A — Capture and extraction quality

```yaml
task_id: capture-extraction-quality
owner: pipeline+capture-ui
base_sha: {{BASE_SHA}}
allowed_paths:
  - services/pipeline
  - apps/web/src/screens/CaptureScreen.tsx
  - apps/web/src/components/ProposalReview.tsx
  - apps/web/src/components/TranscriptPanel.tsx
  - apps/web/src/components/CreatedRecords.tsx
  - apps/web/src/lib/captureFlow.ts
  - apps/web/src/lib/captureFlow.test.ts
read_first: [CLAUDE.md, docs/spec/product.md, docs/spec/interface.md]
```

Goal: a user who types or says several things in one breath gets every item
proposed, correctly titled, with unknowns left `null`.

1. Make `_fixture_extraction` split a note into sentences/clauses and emit one
   proposal per actionable clause (event with fixed time, task with deadline,
   reminder with time, plain todo). Keep entity names ("Boston half marathon"),
   parse "by March 3rd", "next Tuesday at 3", "tomorrow at 6pm" relative to the
   request's timezone/clock; never guess a missing value. Every item carries
   `source_id` + `source_version` + `span_ref`. Add table-driven tests with the
   exact note above as a golden case.
2. Do NOT present fixture output as real. Keep the fixture label. Separately,
   check the Bedrock client path is complete and gated by `METIS_MODE=cloud`
   and budget reservation; do not call AWS. Report what is needed to switch
   production to real extraction (env, IAM, cost) as a handoff note.
3. Capture UI: hide the "Transcribing" step for typed notes; clear the textarea
   after a successful send; after save show "Saved N items" with links to
   Inbox/Tasks and a "Capture another" button; keep focus management intact.
4. Review card: show a readable title instead of `extract-1`; say "sample
   extraction" rather than "fixture-inference" in user text.

Acceptance: `make test` and `make check` pass; golden note yields 4 proposals;
unknown dates stay `null`; no contract edits (propose via handoff).

## Prompt B — Web shell, navigation, copy, mobile and a11y

```yaml
task_id: web-shell-polish
owner: frontend
base_sha: {{BASE_SHA}}
allowed_paths:
  - apps/web/src/app
  - apps/web/src/screens (all except CaptureScreen.tsx)
  - apps/web/src/components (all except ProposalReview, TranscriptPanel, CreatedRecords)
  - apps/web/src/styles.css
  - apps/web/index.html
read_first: [CLAUDE.md, docs/spec/interface.md, apps/web/HANDOFF.md]
```

Use the installed frontend-design, web-design-guidelines, react best-practice
and webapp-testing skills if present; say so if not.

1. Routing: unknown hash shows a small "page not found" with a link to Today
   (do not silently show Today). Set `document.title` per screen
   ("Inbox · Metis"). Keep `#/alerts`; accept `#/notifications` as an alias.
2. Replace raw ids and jargon with human text: source link label becomes
   "From your note · <short date>" (full id only in the evidence popover);
   drop "planning epoch", "r1"; rewrite "Server state: …" plainly while staying
   honest ("Reminders are not confirmed with the scheduler yet").
3. Fix accessible names on every sidebar/bottom-bar button and icon button;
   verify with the accessibility tree, not just visual review.
4. Mobile: test at 390x844 and 768x1024 in a real browser (Playwright, not a
   resized desktop window). Fix overflow, tap targets (>=44px), bottom bar,
   sticky capture button, textarea keyboard overlap, safe-area insets.
5. Empty/first-run states: Today first run shows one friendly "Capture your
   first thought" card instead of a warning banner; the free-time warning shows
   only once tasks exist. Search gets three clickable example questions and an
   honest no-results state. Projects empty state explains how projects appear
   without saying "this build cannot".
6. Lighthouse (mobile) on the deployed build: record perf/a11y/best-practices
   before and after in your handoff.

Acceptance: `make check`, web unit tests, zero console errors, keyboard-only
walk of all 8 screens, before/after screenshots at both widths.

## Prompt C — Backend gaps the UI keeps hitting

```yaml
task_id: backend-user-gaps
owner: backend
base_sha: {{BASE_SHA}}
allowed_paths: [services/api, services/state, services/scheduler, services/planner]
read_first: [CLAUDE.md, docs/spec/architecture.md, new.md]
```

Contract-first: propose schema changes in a handoff; coordinator lands them
before you code against them. Implement with tests:

1. Reminder reschedule (revision + `Idempotency-Key`, outbox row,
   `pending_schedule` until the scheduler confirms).
2. `PATCH /v1/projects/{id}` and `POST /v1/projects` (manual create) so the
   Projects screen is not a dead end.
3. Attach an existing capture/source to a project.
4. Web push: publish the VAPID public key when configured; otherwise a clear
   machine-readable "not configured" so the UI can say so.
5. Decide whether to gate `/v1/health` and `/v1/limits`; document it.
6. Audit every mutation for `revision` -> 409 and outbox atomicity; add missing
   tests. Ownership always from the authenticated subject.

Acceptance: `make test`, `make check`; no change outside owned paths; handoff
lists each new route with request/response examples for the frontend worker.

## Prompt D — Black-box QA, e2e and performance (report-first)

```yaml
task_id: user-journey-qa
owner: qa
base_sha: {{BASE_SHA}}
allowed_paths: [tests/e2e, ops]
read_first: [CLAUDE.md, docs/spec/delivery.md, docs/spec/product.md]
```

Behave as a first-time user, not a developer. Do not edit product code; file
findings and add regression tests.

1. Playwright suite in `tests/e2e/web/` against `METIS_BASE_URL` (default local
   `make dev`; the deployed URL read-only unless told otherwise). Journeys:
   first visit -> Open Metis; type multi-item note -> review -> save ->
   Inbox/Tasks/Today reflect it; complete/defer/cancel a task; reminder due ->
   Alerts feed; settings save with 409 conflict; export request; offline then
   reconnect (outbox replay, no duplicates); reload mid-extraction;
   double-click Send (idempotency); 25 MB+ upload rejection; microphone
   permission denied.
2. Run each journey at desktop, 390x844 and 768x1024, light and dark.
3. Collect console errors, failed network calls, layout overflow, axe-core
   violations and Lighthouse numbers. Rank by user pain (blocker / annoying /
   polish) in `ops/qa-report-<date>.md` with screenshots and repro steps.
4. Confirm and track (do not fix): single-item extraction from the multi-item
   golden note, unknown-route fallback, unnamed nav buttons, raw `src_...` ids,
   always-"Metis" tab title.
5. Add a CI-safe subset (local mode, controllable clock) to `make test`.

Acceptance: suite green locally; report committed; each finding has an owner
(A/B/C) and a one-line repro.

## Merge order

A, B, C, D run in parallel. Coordinator merges C first (contracts/routes), then
A and B, then D reruns the full suite and updates the report.
