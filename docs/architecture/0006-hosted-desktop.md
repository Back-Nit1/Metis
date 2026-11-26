# ADR 0006 — server-authoritative desktop

Accepted 2026-11-26 at the user's direction. Supersedes ADR 0004 decisions
1, 2, 4, 5, 6 and 8 for the shipped desktop app. The local stack remains a
development/test tool, not the default product runtime.

The desktop records microphone audio and displays server results. It starts
no Python API, background worker, Whisper or Ollama process and opens no
application database. Production packaging no longer bundles the Python
backend or model weights. Native capture and browser capture use the same
authenticated HTTPS API; dictation also goes through that API. TLS peer
verification is enabled; native requests reject foreign origins and redirects.

The server owns the database, audio objects, transcripts, extraction,
retrieval, planning and timers. `METIS_MODE=server` selects the existing
SQLite/filesystem adapters on the GCP VM without requiring AWS services.
This is a single-server deployment, not a horizontally scalable database.
Moving to PostgreSQL/Cloud SQL remains a separate adapter/migration decision;
dozens of accounts do not imply dozens of simultaneous model decodes.

Google Identity Platform/Firebase ID tokens identify each account. The API
verifies Google's signature, project audience, issuer, lifetime and subject.
No shared VM access key reaches clients. The server refuses fixture or desktop
identity in server mode. A user's UID scopes existing state/object operations.
Current token verification does not perform online revocation checks; issued
ID tokens expire normally. Native credentials stay in memory; the UI's refresh
session is stored in sessionStorage. Credentials and transient drafts are not
the authoritative transcript database.

The single speech service admits one owner-bound dictation session at a time;
competing callers receive busy responses. The API derives an opaque owner tag
from the verified subject and the server-only speech secret. Session mutation,
finish and cancellation are owner checked and locked. Idle sessions expire.
Multiple API processes must reach that same speech service. Multiple speech
replicas require a routing/state design before they can be added.

Desktop folder connectors are disabled on the shared server, including queued
ingestion, so remote clients cannot make it read arbitrary VM filesystem paths.
An authenticated file-upload connector can be added separately.

Old local records and the VM's old single-user partition are preserved. A new
account must not automatically acquire either partition. Cutover requires
verified account ownership and an explicit migration; there is no background
desktop database replication hidden behind the UI change feed.

Deployment and real-account verification are separate from implementation.
See `ops/gcp/hosted-desktop.md` for prerequisites and the cutover procedure.
