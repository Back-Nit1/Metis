# Wave 50 — corpus, hybrid retrieval, queue lanes, change feed

Spec: `docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.md`
(decisions D1–D10, §14). Contracts are DONE and frozen for this wave
(commit 95bdd0b + 2e7ea30). Do not edit `packages/contracts/`. If a contract is
wrong, stop and report it in your final message.

Already landed (use, don't re-add):
- Contracts: `Connector`, `ConnectorConfig`, `SourceOrigin`, `SourceKind.FILE|EMAIL|DRIVE_FILE`,
  `ConnectorKind`, `ConnectorStatus`, `EventType.CONNECTOR_SYNC_REQUESTED |
  SOURCE_INGEST_REQUESTED | CORPUS_PURGE_REQUESTED`, `INGEST_EVENT_TYPES`,
  `ConnectorSyncPayload`, `SourceIngestPayload`, `PurgePayload`, `Evidence`
  citation fields (`origin, chunk_id, title, uri, start_char, end_char, matched`),
  `SearchResult` (memory_id/chunk_id/kind optional), `CreateConnectorRequest`,
  `UpdateConnectorRequest`, `LaneStats`, `QueueStats`, `DeadLetter`,
  `DeadLetterPage`, `Change`, `ChangesResponse`, `ConnectorPage`.
- Limits: `worker_processes`, `ingest_max_in_flight`, `changes_wait_seconds`,
  `corpus_max_file_bytes`, `retrieval_corpus_share_pct`.
- `SqliteStateStore.list_connectors(user_id, cursor, limit) -> Page[Connector]`,
  `SqliteStateStore.due_connector_ids(now, limit) -> list["user_id/connector_id"]`
  (active, `next_sync_at` passed or null).
- Deps in metis-retrieval: `langchain-core`, `langchain-text-splitters`,
  `langchain-ollama`, `pypdf`. Do not add `langchain-classic` / `langchain-community`.

## Interfaces between agents (exact signatures)

### Agent A — queue (owns `services/state/`, `services/pipeline/src/metis_pipeline/{dispatcher,local_worker}.py`, `apps/desktop/python-backend/`)

`SqliteStateStore` adds:
```python
def claim_outbox(self, *, owner: str, limit: int, lease_seconds: int,
                 lane: Literal["all", "interactive", "ingest"] = "all",
                 max_in_flight: int | None = None) -> list[OutboxEvent]
def queue_stats(self, user_id: str | None = None) -> QueueStats
def list_dead_letters(self, limit: int = 50, *, user_id: str | None = None) -> list[OutboxEvent]
def requeue_outbox(self, user_id: str, event_id: str) -> bool   # dead_letter -> pending, attempts=0, available now
def list_changes(self, user_id: str, *, after: int, limit: int = 500) -> tuple[list[Change], int]
    # returns (changes with seq > after, cursor=max seq seen or current max). Every
    # _put_locked and tombstone appends one row in the same transaction.
def max_change_seq(self, user_id: str) -> int
```
`metis_pipeline.dispatcher.lane_of(event_type: str) -> Literal["interactive","ingest"]`.
`OutboxDispatcher(registry, context, *, lane="all")`.
Worker: `local_worker.main(["--processes", "N"])`; process 0 = interactive lane
(+ reminders, daily plans, and `metis_retrieval.connectors.enqueue_due_syncs(state, clock) -> int`,
lazily imported, ImportError tolerated); processes 1..N-1 = ingest lane with
`max_in_flight=settings().limits.ingest_max_in_flight`.

### Agent B — retrieval (owns `services/retrieval/`)

```python
# metis_retrieval.connectors
DEFAULT_EXCLUDES: tuple[str, ...]
def enqueue_sync(state, *, user_id: str, connector_id: str, clock, full: bool = False,
                 correlation_id: str | None = None) -> str   # returns outbox event id
def enqueue_due_syncs(state, clock) -> int
def remove_connector(state, *, user_id: str, connector: Connector, clock) -> int
    # tombstones connector + its sources, enqueues CORPUS_PURGE_REQUESTED per source

# metis_retrieval.service.RetrievalService
def search(self, user_id: str, *, query: str, project_id: str | None = None,
           limit: int = 20, include_historical: bool = False,
           origins: Sequence[str] = ()) -> tuple[list[SearchResult], str | None]
    # hybrid (semantic + keyword) over memory lane and corpus lane, citations in .evidence
```
Handlers registered on `metis_retrieval.handlers.registry` for the three ingest events.

### Agent C — API (owns `services/api/`) — wave 2, after A and B merge

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| GET | `/v1/connectors` | – | `ConnectorPage` |
| POST | `/v1/connectors` | `CreateConnectorRequest` + `Idempotency-Key` | `Connector` (201), enqueues full sync |
| PATCH | `/v1/connectors/{id}` | `UpdateConnectorRequest` | `Connector`; 409 on revision |
| DELETE | `/v1/connectors/{id}` | – | 204 |
| POST | `/v1/connectors/{id}/sync` | `Idempotency-Key` | `JobAccepted` (202, job_id = outbox event id) |
| GET | `/v1/queue` | – | `QueueStats` |
| GET | `/v1/queue/dead-letters` | – | `DeadLetterPage` |
| POST | `/v1/queue/dead-letters/{event_id}/retry` | – | 202 `{}` |
| GET | `/v1/changes?after=<int>&wait=<0..25>` | – | `ChangesResponse` (long-poll, returns early on change) |
| POST | `/v1/search` | `SearchRequest` | `SearchResponse` via `RetrievalService.search` |

### Agent D — web (owns `apps/web/`)

Builds against the table above. `sync.worker.ts` is a dedicated Web Worker; no
polling loops on the main thread.

### Agent E — desktop (owns `apps/desktop/src-tauri/`, `apps/desktop/scripts/`)

Rust shell env + bundle targets + folder picker command.

## Rules for every agent

- Stay inside your paths. Never `git add -A`. Commit explicit paths, often.
- Run your package's tests + `uv run ruff check` / `ruff format --check` (Python) or
  `pnpm --filter @metis/web typecheck && test` (web) or `cargo check` (Rust) before finishing.
- Honest states (CLAUDE.md rule 3), nothing invented (rule 4), ownership from the server (rule 2).
- Final message: files changed, tests run with counts, anything deferred, any contract problem.
