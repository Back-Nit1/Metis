# Metis local corpus + connectors — design

Status: **draft** 2026-11-21. Needs coordinator review (touches `packages/contracts/`).
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
| `apps/desktop` (Tauri, Rust) | Primary product. Window, HUD, capture, sidecar supervisor, folder picker, OAuth browser hand-off. |
| `apps/web` (React build) | **Stays** — it is the desktop UI (`frontendDist: ../../web/dist`). Still runnable in a browser against the local API for dev. |
| Hosted web + AWS stack | Frozen at current capability. No corpus, no connectors. Kept for capture-from-phone later or retired — open question Q1. |
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

OAuth refresh tokens live in the **macOS Keychain** (Python `keyring`,
service `metis.connector.<id>`). Never in SQLite, never in the webview,
never in logs. Rule 8 (no credentials in the browser) holds.

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
| `gmail` | `users.messages.list` with label/query filter, bounded backfill window (default 1 year) | `users.history.list` from stored `historyId`; full resync if 404 | `messagesDeleted` → tombstone |
| `gdrive` | `files.list` in chosen folders, Google Docs exported as `text/plain` | `changes.list` with stored `pageToken` | `removed`/trashed → tombstone |

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

Desktop must ship a real embedding model. Options:

- **A (recommended): in-process ONNX** via `fastembed` with a small model
  (e.g. `bge-small-en-v1.5`, 384 dim, ~130 MB). Weights fetched at install time
  pinned by SHA-256, same as speech weights (ADR 0004 §5). No extra app for the user.
- B: existing `LocalEmbedder` (Ollama). Zero new code, but user must install
  Ollama and pull a model.

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

## 10. Delivery phases

| Phase | Scope | Gate |
| --- | --- | --- |
| 0 | Fix today: wire span-window chunking into `_record_memory`; desktop uses a real embedder or shows "keyword only" | retrieval eval (`evaluate.py`) improves on paraphrase queries |
| 1 | Contracts (Connector, SourceOrigin, CorpusChunk, events); sqlite-vec store; corpus lane + RRF | unit + eval green; memory-only behaviour unchanged |
| 2 | `local_folder` connector + parsers + chunker + Sources UI | index a 5k-file folder; incremental resync < 30 s with no changes |
| 3 | Gmail connector + OAuth + keychain | backfill 1 year; history sync; delete propagates |
| 4 | Drive connector | changes feed; Docs export; trash propagates |
| 5 | Opt-in extraction from corpus ("make tasks from this") | budget + proposal review path, no auto-created tasks |

## 11. Open questions

- **Q1** Hosted web/AWS stack: keep as phone-capture inbox that syncs to desktop, or retire? Affects whether contracts must stay DynamoDB-compatible for new entities.
- **Q2** Embedding: fastembed in-process (A) vs Ollama (B)? Affects bundle size (~130 MB) and onboarding.
- **Q3** Default Gmail backfill window and label filter (all mail vs Inbox/Sent only).
- **Q4** Multi-device: is the desktop install the single source of truth, or do we need sync between Macs later? (This design assumes single device.)
- **Q5** Windows/Linux in scope, or macOS only for now? (Keychain + folder defaults differ.)
