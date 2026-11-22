# Metis local corpus + connectors — design

Status: **draft** 2026-11-21, decisions D1–D10 recorded §12, build notes §14. Needs coordinator review (touches `packages/contracts/`).
Supersedes nothing yet; would become ADR 0006 once accepted.

## 1. Problem

Metis is meant to be a local-first assistant that knows what is on your
computer and in your accounts. Today retrieval only knows what you *said*:

- Only `audio` and `text` sources exist; `image`/`document` are rejected
  (`services/api/src/metis_api/services.py:134`).
- The only indexed unit is a `Memory`: an LLM-extracted `title + body` created
  when a proposal is accepted (`proposals.py:_record_memory`). The span-window
  chunker in `metis_retrieval/ingestion.py:memories_from_proposal` is unused.
- Desktop runs `METIS_MODE=local` with the default `fixture` embedder (hashed
  bag of words). Effective retrieval on desktop = FTS5 keyword match.
- Vectors are JSON text in SQLite, scanned linearly — fine for hundreds of
  memories, not for 100k file/email chunks.
- No connectors (filesystem, Gmail, Drive) exist anywhere.

## 2. Goals / non-goals

Goals
1. Desktop app is the primary product. It indexes chosen local folders, Gmail
   and Google Drive into a local corpus, on-device by default.
2. Planner and search cite corpus evidence with the same honesty rules as
   memories (`source_id` + `source_version` + `span_ref`, hydrated before use).
3. Incremental sync; deletes upstream become tombstones + vector purge locally.
4. Real semantic retrieval on desktop with no cloud account.

Non-goals (this spec)
- Auto-extracting tasks/events from every file or email (cost + invention risk).
- Write-back to Gmail/Drive.
- Connectors in the hosted/cloud deployment.
- Images/OCR, audio files on disk, calendar sync (separate specs).

## 3. Decision: platform shape

| Surface | Role after this change |
| --- | --- |
| `apps/desktop` (**Rust**, Tauri v2) | Primary product, written in Rust. The Rust shell owns the native layer. Window, HUD, capture, sidecar supervisor, folder picker, OAuth browser hand-off. |
| `apps/web` (React build) | **Stays** — it is the desktop UI (`frontendDist: ../../web/dist`). Still runnable in a browser against the local API for dev. |
| Hosted web + AWS stack | **Retired** (decision D1). No new features; teardown is a separate, explicit ops step. New entities need not be DynamoDB-compatible. |
| Python backend sidecar | Owns connectors, parsing, chunking, embedding, sync jobs. Already has filesystem access. |

Rationale: the browser never needed disk access — the local Python sidecar
does the work. Keeping connectors in Python (not Rust) reuses the existing
outbox/worker/retrieval code and keeps one ingestion path.

## 4. Data model

Two retrieval lanes, deliberately separate:

- **Memory** (unchanged meaning): your own distilled thoughts, decisions,
  attempts. Small, high signal.
- **Corpus chunk** (new): verbatim slices of imported content. Large, noisy.

Mixing them in one table would let 50k emails drown your own decisions.

### 4.1 Contract changes (proposal for coordinator)

```text
SourceKind            + file | email | drive_file
SourceArtifact        + origin: SourceOrigin | None
                      + extracted_text_ref: ObjectRef | None   # text > 100k chars lives in objects dir
SourceOrigin (new)    connector_id, external_id, external_version,
                      uri (path / gmail msg id / drive file id), title,
                      mime_type, modified_at, author | None
Connector (new)       id, user_id, kind (local_folder | gmail | gdrive),
                      status (active | paused | needs_auth | error),
                      config (paths / labels / folder ids, include/exclude globs),
                      cursor (opaque: fs scan stamp | gmail historyId | drive pageToken),
                      last_sync_at, last_error, item_count,
                      allow_cloud_models: bool = false
CorpusChunk (new)     id, user_id, source_id, source_version, span_ref,
                      start_char, end_char, heading_path, content (<= 4000),
                      content_hash, embedding_model_id, embedding_dimensions,
                      indexed_at, validity
EventType             + CONNECTOR_SYNC_REQUESTED, SOURCE_CHUNK_REQUESTED
                      (purge reuses MEMORY_PURGE_REQUESTED pattern -> CORPUS_PURGE_REQUESTED)
Evidence              + origin_kind: memory | corpus   (UI labels "from your email", etc.)
```

Rules preserved: chunk without source is invalid; `source_version` bumps when
`external_version` changes; older-version chunks are dropped at hydration
exactly like memories (`_source_is_current`).

### 4.2 Secrets

OAuth refresh tokens live in the **OS credential store** via Python `keyring`
(service `metis.connector.<id>`): macOS Keychain, Windows Credential Locker,
Linux Secret Service (GNOME Keyring / KWallet). If no Linux secret service is
running, the connector refuses to save the token and says so — no plaintext
fallback. Never in SQLite, never in the webview, never in logs. Rule 8 (no
credentials in the browser) holds.

## 5. Pipeline

```text
Connector sync job  ──► SourceArtifact (+origin) ──► extract text ──► chunk ──► embed ──► corpus index
   (worker, per connector)     upsert by external_id    parsers       structural   local model   sqlite-vec + FTS5
```

All steps are outbox events handled by the existing local worker; each is
idempotent on `(connector_id, external_id, external_version)` and
`content_hash`.

### 5.1 Connectors

| Connector | Discovery | Incremental | Delete |
| --- | --- | --- | --- |
| `local_folder` | user picks folders in Tauri dialog; walk with include/exclude globs | poll every N min: compare `(mtime, size)`, then sha256 | missing path → tombstone |
| `gmail` | `users.messages.list` with `q=after:<epoch>`, backfill window = last 30 days, computed at sync time (§5.1.1) | `users.history.list` from stored `historyId`; on 404 re-backfill with a freshly computed window | `messagesDeleted` → tombstone |
| `gdrive` | `files.list` in chosen folders, Google Docs exported as `text/plain` | `changes.list` with stored `pageToken` | `removed`/trashed → tombstone |

#### 5.1.1 Gmail backfill window (D3)

The window is never a stored or hard-coded date. It is computed from the
current time every time a backfill starts:

```python
from datetime import timedelta

GMAIL_BACKFILL = timedelta(days=30)


def gmail_backfill_query(clock: Clock) -> str:
    # clock.now() is timezone-aware UTC (datetime.now(UTC) in production,
    # a controllable clock in tests/e2e). Gmail's after: accepts epoch seconds,
    # which sidesteps the mailbox's timezone entirely.
    since = clock.now() - GMAIL_BACKFILL
    return f"after:{int(since.timestamp())}"
```

- Uses the injected `Clock` port (`metis_contracts.ports.Clock`), which wraps
  `datetime.now(UTC)`, rather than calling `datetime.now()` inline. Same
  real-time behaviour, and e2e tests can pin the clock.
- "Past month" = rolling 30 days, not calendar month (no month-length edge cases).
- First sync stores the newest `historyId`; after that only `history.list`
  runs, so mail is never re-imported. Re-backfill (expired `historyId`,
  reconnect) recomputes the window from *now*.
- Mail older than 30 days is not imported. Widening the window later is a
  config value, not a code change.

Ponytail: polling first for local folders. FSEvents watcher only if polling
latency is a real complaint.

Default exclude list (non-overridable for secrets): `.ssh/`, `.gnupg/`,
`.env*`, `*.pem`, `*.key`, Keychains, `.git/`, `node_modules/`, `.venv/`,
`Library/`, files > 20 MB, binaries by sniffed mime type.

### 5.2 Parsing

- Plain text, Markdown, code, CSV: read as UTF-8 (fallback latin-1).
- PDF: `pypdf` text layer only; scanned PDFs marked `failure=no_text_layer` (honest, not OCR'd).
- DOCX: `python-docx`.
- Email: `text/plain` part (HTML stripped if only part); quoted reply chains
  and signatures stripped; header block (from, to, date, subject) kept as chunk context.
- Unsupported → source stays with `failure=unsupported_media`, visible in UI.

### 5.3 Chunking

- Split on structure first (headings, paragraphs, email messages), then pack to
  ~500 tokens with ~60 token overlap.
- Each chunk content is prefixed with its context line
  (`<title> › <heading path>` or `Email: <subject> — <from>, <date>`) so a
  chunk stands alone.
- `span_ref = chunk:<start_char>-<end_char>` into the extracted text of that
  `source_version`.

Same pass fixes the memory gap: `_record_memory` switches to
`memories_from_proposal`'s span context window.

### 5.4 Embeddings

**Decision D2: Ollama.** Reuses the existing `LocalEmbedder`
(`/api/embed` over loopback, dimension checked on every response).

- Ollama runs natively on macOS, Windows and Linux; the user installs it once.
- Default model `nomic-embed-text` (768 dim, ~270 MB). Set by the desktop
  supervisor as `METIS_LOCAL_EMBED_MODEL`; user can override.
- Embedder selection is **decoupled from the LLM engine**: new
  `METIS_EMBED_ENGINE` (`auto|fixture|local`), so local embeddings don't force
  local extraction. Today `build_embedder()` follows `METIS_INFERENCE_ENGINE`.
- First run: desktop probes `127.0.0.1:11434`. Missing Ollama → Sources screen
  says "install Ollama for semantic search" with the download link; missing
  model → offers `ollama pull nomic-embed-text`. Until then, keyword-only.
- Ollama down mid-sync → chunks stay `pending_embedding`, retried by the
  outbox; FTS5 keyword search still works on them.

Fixture embedder stays for tests only. If desktop is running fixture or the
model is missing, UI must say "keyword search only" (rule 3, honest states).

One `model_id` per index; changing model triggers a background re-embed, and
queries never mix dimensions (already enforced).

### 5.5 Vector storage

Replace JSON-text vectors with **`sqlite-vec`** (`vec0` virtual table, float32)
in `memory.sqlite` for both lanes. Keeps one local file, adds KNN without a
server. Linear scan over JSON would parse every vector on every query — not
viable past ~10k chunks.

## 6. Retrieval changes

`RetrievalService.retrieve_supporting_memories` gains a corpus lane:

1. Query both lanes (vector + FTS5 each).
2. Fuse within lane by reciprocal rank fusion (replaces `max(semantic, lexical)`).
3. Merge lanes with a quota: memories first-class, corpus capped at
   `retrieval_corpus_share` (default 50%) of candidates.
4. Hydrate every hit against authoritative state (source exists, not
   tombstoned, version current, owner matches).
5. `EvidenceBudget` unchanged — one plan-wide token budget.

Planner prompt labels corpus evidence by origin so it can't present an email
excerpt as the user's own decision.

## 7. Model calls and privacy

- Indexing (parse, chunk, embed) makes **zero paid model calls** on desktop.
- LLM extraction over corpus content happens only on explicit user action
  ("extract tasks from this email/file") or opt-in rule per connector; it goes
  through reserve/settle budgets (rule 7).
- If `METIS_INFERENCE_ENGINE=cloud`, corpus text is sent to Bedrock only when
  the connector has `allow_cloud_models=true`. Default false.
- Connector removal = purge sources, chunks, vectors and keychain entry.

## 8. Google OAuth

- Google "Desktop app" OAuth client, PKCE, loopback redirect to
  `127.0.0.1:<ephemeral>` handled by the Python API; Tauri opens the system browser.
- Scopes: `gmail.readonly`, `drive.readonly` (read-only, nothing broader).
- **Risk:** both are restricted scopes. Public distribution needs Google app
  verification + annual CASA security assessment (cost and weeks of lead time).
  Until then: testing mode, ≤100 named test users, tokens expire every 7 days
  in testing mode — UI must surface `needs_auth` honestly.

## 9. UI (desktop)

- Settings → Sources: add folder / connect Gmail / connect Drive; per-connector
  status, item count, last sync, errors, pause, remove (with purge).
- Search results and plan evidence show origin chip (Memory / File / Email / Drive)
  and open the original (file path reveal, Gmail/Drive deep link).
- Index status: "Indexing 1,240 / 8,000", "keyword only", "needs re-auth".

## 10. Cross-platform: macOS, Windows, Linux (D5)

Metis ships as a **native installer per OS**. Everything in the stack is
already portable:

| Layer | macOS | Windows | Linux |
| --- | --- | --- | --- |
| Shell (Tauri v2) | `.app` / `.dmg` | `.msi` / NSIS `.exe` | `.deb` / `.AppImage` |
| Backend sidecar (PyInstaller, per target triple, `build.sh` already handles `.exe`) | arm64 + x86_64 | x86_64 | x86_64 |
| Embeddings | Ollama | Ollama | Ollama |
| Secrets | Keychain | Credential Locker | Secret Service |
| App data | `~/Library/Application Support/Metis` | `%APPDATA%\Metis` | `$XDG_DATA_HOME/metis` |

Work needed:
- `tauri.conf.json` `bundle.targets` is `["app"]` (macOS only) → add
  `dmg, msi, nsis, deb, appimage`.
- Rust shell OS-specific code: audio capture, global hotkey, text insertion
  (`insertion.rs`), HUD window behaviour. Each needs a Windows/Linux path or an
  honest "not supported on this OS" state. Linux Wayland restricts global
  hotkeys and synthetic typing — likely degraded there.
- Paths through `pathlib` only; default exclude list per OS (§5.1).
- CI matrix (GitHub Actions `macos-14`, `windows-latest`, `ubuntu-22.04`)
  builds and tests each installer. PyInstaller cannot cross-compile, so each OS
  must build its own sidecar.

### 10.1 Docker — where it fits, where it doesn't

Requested: use Docker for multi-OS support. **Recommendation: do not run the
end-user app in Docker.** Reasons:

- On macOS and Windows, Docker runs a Linux VM. Your files, microphone,
  keychain, global hotkeys and text insertion are all on the host. A container
  can't reach them without bind mounts and device passthrough, and Windows and
  macOS don't support microphone passthrough at all.
- Users would have to install Docker Desktop (~2 GB plus a VM that keeps
  several GB of RAM reserved). That's the memory cost D1 was trying to avoid.
  Docker Desktop also needs a paid licence at larger companies.
- Ollama inside Docker on a Mac gets no GPU (Metal), so embeddings are much slower.

Docker **is** used for:
1. **Reproducible Linux builds.** A `Dockerfile.build` pins Debian, Python,
   uv, Rust and the Tauri dependencies to produce the Linux sidecar and
   `.deb`/`.AppImage`. CI and devs get identical artefacts.
2. **Headless dev/test stack.** `docker compose up` runs the API, worker,
   speech (existing `services/speech/Dockerfile`) and Ollama with fixture
   adapters, so contributors on any OS can run backend tests without a local
   Python setup. Dev only; never shipped to users.
3. **Optional power-user mode (later, not in scope).** Linux users who want
   the backend in a container on a home server. Single-device rule (D4) still holds.

## 11. Delivery phases

| Phase | Scope | Gate |
| --- | --- | --- |
| 0 | Fix today: wire span-window chunking into `_record_memory`; `METIS_EMBED_ENGINE` + Ollama probe in desktop, "keyword only" otherwise | retrieval eval (`evaluate.py`) improves on paraphrase queries |
| 0b | Cross-platform builds: bundle targets, CI matrix, `Dockerfile.build` for Linux, dev `docker-compose.yml` | installers build and launch on all three OSes; backend tests pass in compose |
| 1 | Contracts (Connector, SourceOrigin, CorpusChunk, events); sqlite-vec store; corpus lane + RRF | unit + eval green; memory-only behaviour unchanged |
| 2 | `local_folder` connector + parsers + chunker + Sources UI | index a 5k-file folder; incremental resync < 30 s with no changes |
| 3 | Gmail connector + OAuth + keychain | backfill 1 year; history sync; delete propagates |
| 4 | Drive connector | changes feed; Docs export; trash propagates |
| 5 | Opt-in extraction from corpus ("make tasks from this") | budget + proposal review path, no auto-created tasks |

## 12. Decisions (2026-11-21)

- **D1** Hosted web/AWS stack (phone-capture inbox) retired. Teardown of the
  deployed AWS resources is a separate, explicitly approved ops step.
- **D2** Embeddings via Ollama (`nomic-embed-text` default), see §5.4.
- **D3** Gmail backfill = rolling last 30 days, computed from the current time
  at every backfill (§5.1.1); then incremental via `historyId`.
- **D4** Single device. The desktop install is the only source of truth; no
  cross-device sync.
- **D5** macOS, Windows and Linux. Native installers; Docker only for builds and
  the dev stack (§10.1).

- **D6** The desktop app is **Rust** (Tauri v2). The React build is only its
  view layer; native capability (audio, hotkeys, insertion, process
  supervision, OS integration) stays in Rust.
- **D7** **LangChain** is the retrieval vocabulary: `langchain-core`
  (`BaseRetriever`, `Document`, `Embeddings`), `langchain-text-splitters`
  (`RecursiveCharacterTextSplitter`) and `langchain-ollama`
  (`OllamaEmbeddings`). Not `langchain-classic`/`langchain-community`: they
  pull SQLAlchemy, LangSmith and hundreds of loaders into the bundled sidecar.
  Rank fusion is the same weighted reciprocal-rank-fusion `EnsembleRetriever`
  uses, implemented inside our own `HybridRetriever`.
- **D8** **Hybrid retrieval everywhere.** Every search and every planner
  evidence lookup runs semantic (vector cosine) **and** keyword (SQLite FTS5
  BM25) search, then fuses them. Keyword search covers file names and paths
  too (the `find -iname "*report*"` case), so a file is findable by name even
  when its body never mentions the word. Every result is a **citation**: title,
  path/URI, excerpt, character span, source id + version, and which matchers
  hit (`semantic`, `keyword`).
- **D9** **Queued ingestion with lanes and a dead-letter queue.** All data
  streams (captures, files, PDFs, email, Drive) flow through the one
  transactional outbox. Events are split into two lanes:
  `interactive` (captures, extraction, reminders, plans) and `ingest`
  (connector sync, file/email parsing, chunk embedding). Several Python worker
  processes claim from the outbox with leases; the `ingest` lane has a global
  in-flight cap so a 10k-file import can never starve reminders. Malformed
  input (corrupt PDF, undecodable mail, unsupported type) is a permanent
  failure and goes straight to `dead_letter`, no retries. Transient failures
  (Ollama down) back off and retry, then dead-letter. The dead-letter queue can
  be listed and retried from the API and the Sources screen.
- **D10** **Background sync, never on the UI thread.**
  - Backend: the worker re-syncs every active connector on an interval and
    keeps the corpus at the current state of each stream.
  - Frontend: a dedicated **Web Worker** (`sync.worker.ts`) long-polls
    `GET /v1/changes?after=<cursor>` and posts coalesced change notices
    (which kinds changed, queue counts) to the page. The page only re-reads
    the data it is showing. No polling loops on the main thread.
  - Change feed: a `changes` table (autoincrement sequence) appended in the
    same transaction as every entity write and tombstone.

## 13. Still open

- **Q6** With AWS retired, LLM extraction on desktop has no Bedrock. Choose:
  local Ollama LLM (free, slower, weaker), or direct Anthropic API with a
  user-supplied key stored in the OS credential store.
- **Q7** Linux Wayland: accept degraded hotkey/insertion, or target X11 only at first?

## 14. Build notes (first slice)

Built in this order:
1. Queue lanes, in-flight cap, multi-process worker (`--processes N`),
   dead-letter list/retry API.
2. Corpus: `local_folder` connector, parsers (text/markdown/code/PDF),
   LangChain splitter + Ollama embeddings, corpus index (vectors + FTS5 over
   content, title and path), `HybridRetriever`, citations in `/v1/search` and
   planner evidence.
3. Change feed + `sync.worker.ts` + Sources screen (folders, queue, dead letters).

Deferred: Gmail/Drive connectors (need a Google OAuth client id and the
verification decision in §8), `sqlite-vec` (exact float32 scan first; swap
when a corpus is large enough to measure), Windows/Linux Rust ports (§10).
