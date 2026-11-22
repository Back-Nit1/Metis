# Agent B — Web shell, navigation, copy, mobile and accessibility

You are Agent B of four parallel workers. Work in your own git worktree branched
from `27a89a105a2297fad5a685d74a04b16dee07c89d` (branch `agent-b-shell`).
Read `CLAUDE.md`, `docs/spec/interface.md`, `apps/web/HANDOFF.md` first.
Use the frontend-design, web-design-guidelines, vercel-react-best-practices and
webapp-testing skills if available; if one is not, say so and do not claim it.

## Context (real-user walkthrough of https://metis-delta.vercel.app/)

- Unknown hash (`#/notifications`) silently renders Today; the real route is
  `#/alerts`. Tab title is always "Metis".
- Raw ids shown: "from src_0a196c45…" (Inbox, Today).
- Jargon: "planning epoch 0/1", "r1" on task rows, "Server state: Scheduling:
  not confirmed yet".
- Sidebar buttons expose no accessible name in the accessibility tree.
- Only one `@media (max-width: 767px)` block in `styles.css`; mobile never
  tested in a real narrow viewport.
- Today shows a "Free time is unknown" warning to a brand-new user.
- Search is blank with no guidance. Projects empty state says "This build
  cannot create one by hand."

## Owned paths (touch nothing else)

- `apps/web/src/app/`
- `apps/web/src/screens/` EXCEPT `CaptureScreen.tsx` (Agent A owns it)
- `apps/web/src/components/` EXCEPT `ProposalReview.tsx`, `TranscriptPanel.tsx`,
  `CreatedRecords.tsx` (Agent A)
- `apps/web/src/styles.css`, `apps/web/index.html`

Never edit `packages/contracts/`, `docs/`, manifests or lockfiles. Backend
routes you want (project create/update, reminder reschedule) are Agent C's:
code to the contract that exists today, and list what you are waiting on.

## Tasks

1. Routing: unknown hash shows a small "page not found" view linking to Today.
   Accept `#/notifications` as an alias of alerts. Set `document.title` per
   screen ("Inbox · Metis").
2. Copy: source link label becomes "From your note · <short date>" (full id only
   in the evidence popover). Remove "planning epoch" and "r1". Rewrite "Server
   state: …" plainly but honestly, e.g. "Reminders are not confirmed with the
   scheduler yet". Honest-state rules (CLAUDE.md rule 3) still hold.
3. Accessibility: every sidebar, bottom-bar and icon button gets an accessible
   name. Verify through the accessibility tree and axe-core, not by eye.
4. Mobile: test 390x844 and 768x1024 in real Playwright viewports (a resized
   desktop window is not enough). Fix overflow, tap targets (>=44px), bottom
   bar, capture button, keyboard overlap, safe-area insets. Light and dark.
5. First-run and empty states: Today first run shows one "Capture your first
   thought" card; the free-time warning appears only once tasks exist. Search
   gets three clickable example questions and an honest no-results state.
   Projects empty state explains how projects appear, without "this build
   cannot".
6. Run Lighthouse (mobile) on the deployed URL before you start and on your
   local build after; report perf / accessibility / best-practices numbers.

## Rules

- No AWS keys, Bedrock calls or model ids in `apps/web/` (CLAUDE.md rule 8).
- Keep the existing visual system; do not redesign.
- Stage explicit paths only; never `git add -A`. Commit at safe boundaries,
  label incomplete work `checkpoint:` in the body.

## Done when

`make check` and web unit tests pass; zero console errors on all 8 screens;
keyboard-only walk of all screens works; before/after screenshots at both
widths. Final message (not a docs file) includes changed files, Lighthouse
before/after, screenshots paths, and anything blocked on Agent C.
