# Agent D — Black-box user-journey QA, e2e and performance

You are Agent D of four parallel workers. Work in your own git worktree branched
from `27a89a105a2297fad5a685d74a04b16dee07c89d` (branch `agent-d-qa`).
Read `CLAUDE.md`, `docs/spec/delivery.md`, `docs/spec/product.md` first. Use the
webapp-testing skill if available; if not, say so.

## Role

Act as a first-time user, not a developer. You do NOT fix product code. You find
problems, write regression tests, and produce a ranked report. Other agents are
changing the code in parallel (A: extraction/capture, B: web shell, C: backend),
so expect some failures to flip to green as their work merges.

## Owned paths (touch nothing else)

- `tests/e2e/` (new Playwright suite under `tests/e2e/web/`)
- `ops/`

Never edit product code, `packages/contracts/`, `docs/`, manifests or lockfiles.
If the Playwright dependency needs a manifest change, request it from the
coordinator in your final message and, meanwhile, install it locally without
committing the lockfile.

## Target

`METIS_BASE_URL` env var. Default: local `make dev`. The deployed app
https://metis-delta.vercel.app/ may be used READ-ONLY plus capture of your own
test notes; do not delete data, request exports, or change settings on the
deployed app. Mutating journeys run against local only.

## Known issues to confirm and track (do not fix)

1. Multi-item note yields one item: "Dentist next Tuesday at 3. Register for the
   Boston half marathon by March 3rd. Remind me to call mom tomorrow at 6pm.
   Buy milk." -> expected 4 proposals. Owner A.
2. `#/notifications` silently renders Today. Owner B.
3. Sidebar buttons have no accessible name. Owner B.
4. Raw `src_…` ids shown to users. Owner B.
5. Tab title is always "Metis". Owner B.
6. Typed notes show a "Transcribing" step. Owner A.

## Tasks

1. Playwright suite in `tests/e2e/web/`. Journeys:
   - first visit -> Open Metis -> Today
   - type multi-item note -> review -> save -> Inbox/Tasks/Today reflect it
   - complete / defer / cancel a task
   - reminder comes due -> appears in Alerts (use the controllable clock)
   - settings save, including a 409 conflict from a stale revision
   - export request (local only)
   - go offline, capture, reconnect: outbox replays with no duplicates
   - reload mid-extraction
   - double-click Send: one capture only (idempotency)
   - upload over 25 MB rejected with a clear message
   - microphone permission denied: clear fallback to typing
2. Run each journey at 1280x800, 768x1024 and 390x844, light and dark. Use real
   Playwright viewports and `isMobile` emulation.
3. Collect per journey: console errors, failed network calls, horizontal
   overflow, axe-core violations, screenshots on failure.
4. Lighthouse (mobile) on the deployed URL: performance, accessibility,
   best practices, with the top 3 opportunities.
5. Write `ops/qa-report-2026-10-19.md`: findings ranked blocker / annoying /
   polish, each with owner (A/B/C), one-line repro, expected vs actual, and a
   screenshot path. Include a "what works well" section.
6. Add a CI-safe subset (local mode, fixture adapters, controllable clock, no
   network to the deployed app) and tell the coordinator how to wire it into
   `make test`.

## Rules

- Honest states: a fixture result must be labelled; flag any place it is not.
- Never commit recordings, `.env` or credentials. Screenshots go under
  `ops/qa-artifacts/` and are small.
- Stage explicit paths only; never `git add -A`. Commit at safe boundaries,
  label incomplete work `checkpoint:` in the body.

## Done when

Suite runs green locally for fixed behaviour and fails (tagged `@known-issue`)
only for the confirmed issues above; the report is committed; the final message
summarizes blockers first, then the manifest/CI changes you need from the
coordinator. After A, B and C merge, the coordinator will ask you to rerun and
update the report.
