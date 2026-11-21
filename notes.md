# My to-do tracker

Things only I can do (accounts, secrets, decisions). Tick `[x]` when done.
Last updated: 2026-11-21.

## Now: make the live site private (in this order)

The live site (metis-delta.vercel.app) stays open to anyone until 1-4 are done.

- [ ] **1. Rotate the VM access key.** The old one was usable by anyone through the proxy.
  - [ ] `gcloud --project <id> compute ssh metis --zone us-central1-a --tunnel-through-iap --command 'sudo rm /etc/metis-api.env'`
  - [ ] Check out `fix/vercel-login-gate` (deploy ships the commit I'm on), then `PROJECT=<id> ops/gcp/deploy.sh` (makes a new key)
  - [ ] Read the new key: `... --command 'sudo cat /etc/metis-api.env'`
- [ ] **2. Set Vercel env vars (production)** with `vercel env add NAME production`:
  - [ ] `METIS_API_ORIGIN`: `https://<ip>.sslip.io`
  - [ ] `METIS_ACCESS_KEY`: the new key from step 1 (remove the old value first)
  - [ ] `METIS_LOGIN_USER`: my username
  - [ ] `METIS_LOGIN_PASSWORD_HASH`: `node ops/vercel/hash-password.mjs | vercel env add METIS_LOGIN_PASSWORD_HASH production` (long random password, 12+ chars, save it in a password manager)
  - [ ] `METIS_SESSION_SECRET`: `openssl rand -base64 48`
- [ ] **3. Deploy the UI:** `PROJECT=<id> ops/vercel/deploy.sh`
- [ ] **4. Verify:**
  - [ ] Private window, site shows a login screen
  - [ ] Wrong password is refused; 6th fast try says "too many attempts"
  - [ ] Right password, app works (capture, tasks, export)
  - [ ] `curl https://metis-delta.vercel.app/v1/captures` returns 401
  - [ ] `curl https://<ip>.sslip.io/v1/captures` (no key) returns 401
- [ ] **5. Merge** `fix/vercel-login-gate` into `main`

## Security gaps still open

- [ ] Deleting a capture doesn't delete its transcript
- [ ] `/v1/health` and `/v1/limits` need no login (they show adapter modes and model IDs)
- [ ] Reminders are auto-scheduled from AI extraction with no review. Decide: keep it or require approval
- [ ] Sessions can't be revoked one by one. To sign everyone out, rotate `METIS_SESSION_SECRET`
- [ ] Replace the single login with real accounts (Cognito) before anyone else uses it

## Not built or not proven yet

- [ ] Live site uses fixture (fake) extraction, not a real AI model. Decide: local LLM on the VM, or Bedrock
- [ ] Nothing deployed to AWS; every cloud check is unrun
- [ ] Cognito sign-in never completed end to end
- [ ] Speech accuracy and speed never measured with a real model
- [ ] Desktop app: unsigned, Apple Silicon only, mic/HUD/shortcut never tested by hand
- [ ] Missing UI features: create a project by hand, reschedule reminders, push notification setup (`docs/agent-prompts/43`)
- [ ] D9: real extraction doesn't emit the project hint
- [ ] AWS cost estimate (~$24-26/mo) is above the $25 default budget. Decide: raise to ~$40 or drop interface endpoints

## Housekeeping

- [ ] `.worktrees/` (other agents) has 2 ruff lint errors in `tests/e2e/test_e2e_semantics.py` (unused `import pytest`)
- [ ] Untracked agent prompts `docs/agent-prompts/41-44`: commit or delete
- [ ] `docs/progress.md` last updated 2026-10-07; ask the coordinator to bring it up to date
