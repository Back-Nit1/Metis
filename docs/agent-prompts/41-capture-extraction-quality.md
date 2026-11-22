# Agent A — Capture and extraction quality

You are Agent A of four parallel workers. Work in your own git worktree branched
from `27a89a105a2297fad5a685d74a04b16dee07c89d` (branch `agent-a-capture`).
Read `CLAUDE.md`, `docs/spec/product.md`, `docs/spec/interface.md` first, and
nothing else you do not need.

## Context

A real-user walkthrough of https://metis-delta.vercel.app/ typed this note:

> Dentist next Tuesday at 3. Register for the Boston half marathon by March 3rd.
> Remind me to call mom tomorrow at 6pm. Buy milk.

Metis proposed ONE item titled "Register for the event" with no deadline. It
lost the dentist event, the call-mom reminder, the milk task, the name "Boston
half marathon" and its March 3rd deadline. Production runs
`FixtureInferenceClient` (`services/pipeline/src/metis_pipeline/inference.py`,
`_fixture_extraction`), a regex stub. The UI correctly labels it FIXTURE MODE.

## Owned paths (touch nothing else)

- `services/pipeline/`
- `apps/web/src/screens/CaptureScreen.tsx`
- `apps/web/src/components/ProposalReview.tsx`, `TranscriptPanel.tsx`, `CreatedRecords.tsx`
- `apps/web/src/lib/captureFlow.ts`, `captureFlow.test.ts`

Never edit `packages/contracts/`, `docs/`, manifests or lockfiles. If you need a
schema change, write it in your handoff and stop work on that part.

## Tasks

1. Rework `_fixture_extraction` to split a note into sentences/clauses and emit
   one proposal per actionable clause: event (fixed time), task (optional
   deadline), reminder (time), plain todo. Keep entity names ("Boston half
   marathon"). Parse "by March 3rd", "next Tuesday at 3", "tomorrow at 6pm"
   relative to the request's timezone and clock. Unknown date/time stays `null`;
   never guess. Every item carries `source_id` + `source_version` + `span_ref`.
   Keep the existing status-update behaviour ("already registered", "cancel
   that") and the existing ambiguity/target-choice behaviour working.
2. Add table-driven tests, including the exact note above as a golden case
   expecting 4 proposals with correct kinds, titles and dates.
3. Do not present fixture output as real. Keep fixture labels. Check the Bedrock
   client path is complete and gated by `METIS_MODE=cloud` plus budget
   reservation. Do NOT call AWS. Write a handoff section listing what switching
   production to real extraction needs (env vars, IAM, estimated cost).
4. Capture UI:
   - Hide the "Transcribing" step for typed notes.
   - Clear the textarea after a successful send.
   - After save show "Saved N items" with links to Inbox and Tasks and a
     "Capture another" button.
   - Review card shows a readable title, not `extract-1`; user-facing text says
     "sample extraction", not "fixture-inference".
   - Keep focus management and honest states (CLAUDE.md rules 3 and 4).

## Rules

- `user_id` from the authenticated subject only. No credentials in the browser.
- Local mode must still work with no cloud account.
- Stage explicit paths only; never `git add -A`. Commit at safe boundaries,
  label incomplete work `checkpoint:` in the body.

## Done when

`make test` and `make check` pass; the golden note yields 4 proposals; unknown
values are `null`; handoff written to `docs/handoffs/agent-a.md` is NOT allowed
(docs is coordinator-owned), so put it in your final message instead: changed
files, test results, schema-change requests, real-extraction prerequisites.
