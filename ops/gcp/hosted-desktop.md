# Hosted desktop cutover

The server is `https://34-30-254-78.sslip.io` in project `metis-dev-856337`.
This is the native default; `METIS_SERVER_URL` can override it at build or
launch time with an HTTPS origin. No server secret belongs in a desktop build.

## Prerequisites

1. Enable Google Identity Platform in the existing GCP project and enable
   email/password sign-in. Create each pilot account in its Users console.
   The client supports sign-in, not public self-registration. Copy the public
   web API key and project ID from the provider configuration. Restrict the key
   to Identity Toolkit and Secure Token APIs; ensure restrictions allow the
   desktop client. Reference: [Identity Platform email sign-in](https://cloud.google.com/identity-platform/docs/web/email).
2. Install Ollama on the **VM**, bind it only to `127.0.0.1:11434`, and provision
   an inference model and embedding model there. Choose model sizes within the
   VM's memory budget alongside Whisper. The cutover script checks the models
   are installed; it does not download models or provision compute. Record the
   exact model names and verify a real extraction and embedding before rollout.
3. Merge the implementation to `main`, run checks, and deploy that commit with
   the existing GCP deployment command. Back up and verify the database/object
   store before changing the authentication mode. Keep the existing backups.

The existing VM was verified running during development; neither Identity
Toolkit nor Firebase API appeared enabled in the project. These prerequisites
are not claimed complete by the implementation.

## Server cutover

On the VM, after deploying the main commit:

```bash
sudo env METIS_FIREBASE_PROJECT_ID=metis-dev-856337 \
  METIS_LOCAL_LLM_MODEL='<installed-inference-model>' \
  METIS_LOCAL_EMBED_MODEL='<installed-embedding-model>' \
  bash /opt/metis/ops/gcp/configure-hosted.sh
```

The script writes `/etc/metis-hosted.env` and systemd drop-ins loaded after
legacy environment files. It clears the shared desktop secret **in the running
configuration**, enables production server mode, and allows the desktop/Vercel
origins. It preserves the old secret file and database for controlled rollback.
The overrides survive normal redeployment. Existing single-user rows remain
under their original owner; signing in with Firebase does not reassign them.

To roll back, remove only the `zz-hosted.conf` files installed by this script,
reload systemd, and restart the five listed services. This restores the old
single-user configuration, not a multi-user fallback inside the new client.

## Client builds

```bash
export VITE_METIS_FIREBASE_PROJECT_ID=metis-dev-856337
export VITE_METIS_FIREBASE_API_KEY='<public-web-api-key>'
export VITE_METIS_API=https://34-30-254-78.sslip.io
pnpm --filter @metis/desktop tauri build
# For the browser build/deployment from main:
PROJECT=metis-dev-856337 bash ops/vercel/deploy.sh
```

The hosted Vercel deployment uses direct per-user authenticated GCP requests
and removes the old shared-key login/proxy functions from its deployment
artifact. The legacy flow is available only with `METIS_LEGACY_SINGLE_USER=true`.
Missing desktop identity configuration fails closed with a setup message.

## Verification and existing data

Use two pilot accounts: create text and audio captures in account A, verify
the transcript after restarting the desktop, and verify account B cannot read,
edit, delete or finish A's captures/dictation. Confirm no Python/Whisper/Ollama
process starts on the client, then close the client and verify server timers
continue. Check busy handling with two dictation users and benchmark the VM
before admitting concurrent production users. One busy dictation slot is a
deliberate capacity limit, not evidence of a load-tested multi-user service.

Do not delete `~/Library/Application Support/app.metis.desktop` or copy its
SQLite database over the server database. Existing local history needs an
owner-verified import preserving revisions, IDs/provenance and audio references;
this cutover does not yet implement that import. Export/backup the old
installation before replacement. New captures use the server directly and do
not require desktop-to-server database replication.
