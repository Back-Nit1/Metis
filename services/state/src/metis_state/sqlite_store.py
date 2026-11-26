"""SQLite implementation of :class:`metis_contracts.ports.StateStore`.

Deliberately mirrors the DynamoDB design so both adapters share semantics:

* ``(user_id, kind, id)`` stands in for ``PK = USER#<sub>`` / ``SK = <TYPE>#<id>``
* a write is guarded by ``expected_revision`` and bumps ``revision`` by one
* the entity row, its immutable revision-log entry, the outbox append and any
  planning-epoch bump all commit in one ``BEGIN IMMEDIATE`` transaction
* every read is owner-scoped; there is no "find this id anywhere" path

ponytail: one generic ``records`` table with denormalised query columns instead
of a table per entity. If a query pattern outgrows the indexes here, add a
column plus an index rather than splitting the table.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from metis_contracts import (
    INGEST_EVENT_TYPES,
    AdapterMode,
    ErrorCode,
    Event,
    ExtractionProposal,
    JobState,
    MetisError,
    OutboxEvent,
    Page,
    Plan,
    PlanStatus,
    Preferences,
    ReminderState,
    SourceArtifact,
    SystemClock,
    Task,
    TaskState,
    UsageDay,
    new_id,
    settings,
)
from metis_contracts.api import Change, LaneStats, QueueStats
from metis_contracts.common import Record
from metis_contracts.entities import ENTITY_MODELS, AvailabilityBlock, Job, Notification, Reminder
from metis_contracts.events import EventType

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()

#: Kinds whose change must invalidate an in-flight or proposed plan.
PLANNING_RELEVANT_KINDS = frozenset({"task", "event", "availability", "preferences", "project"})

#: Kinds that never reach the change feed: queue and ledger plumbing, not user data.
CHANGE_FEED_SKIP_KINDS = frozenset({"outbox", "budget_reservation", "usage_day"})

Lane = Literal["all", "interactive", "ingest"]
_INGEST_TYPES = tuple(sorted(INGEST_EVENT_TYPES))
_INGEST_MARKS = ",".join("?" for _ in _INGEST_TYPES)
#: SQL `CASE` labelling an outbox row's lane; params are ``_INGEST_TYPES``.
_LANE_SQL = f"CASE WHEN event_type IN ({_INGEST_MARKS}) THEN 'ingest' ELSE 'interactive' END"


def _lane_filter(lane: Lane) -> tuple[str, tuple[str, ...]]:
    if lane == "ingest":
        return f"AND event_type IN ({_INGEST_MARKS})", _INGEST_TYPES
    if lane == "interactive":
        return f"AND event_type NOT IN ({_INGEST_MARKS})", _INGEST_TYPES
    return "", ()


#: Denormalised query columns, per kind. Everything else lives in the JSON body.
_QUERY_COLUMNS: dict[str, tuple[str, ...]] = {
    "task": ("state", "project_id", "deadline_date"),
    "event": ("project_id", "local_date"),
    "availability": ("local_date",),
    "plan": ("local_date", "state"),
    "reminder": ("state", "trigger_at"),
    "job": ("state",),
    "memory": ("project_id",),
    "proposal": ("state",),
    "project": ("state",),
    "connector": ("state", "trigger_at"),
}


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


class _Tx:
    """Transaction handle. Collects writes and commits them atomically."""

    def __init__(self, store: SqliteStateStore, conn: sqlite3.Connection, user_id: str) -> None:
        self._store = store
        self._conn = conn
        self.user_id = user_id
        self.epoch_bumped = False
        self.written: list[tuple[str, str, int]] = []

    def put[R: Record](self, kind: str, record: R, *, expected_revision: int | None = None) -> R:
        """Insert or update one record. Returns the stored record with its new
        revision. ``expected_revision=None`` means "must not already exist"."""
        if record.user_id != self.user_id:
            raise MetisError(ErrorCode.FORBIDDEN, "record owner does not match the transaction owner")
        stored = self._store._put_locked(self._conn, kind, record, expected_revision)
        self.written.append((kind, stored.id, stored.revision))
        if kind in PLANNING_RELEVANT_KINDS:
            self.bump_planning_epoch(self.user_id)
        return stored

    def append_outbox(self, event: OutboxEvent) -> None:
        if event.user_id != self.user_id:
            raise MetisError(ErrorCode.FORBIDDEN, "outbox event owner mismatch")
        self._store._append_outbox_locked(self._conn, event)

    def bump_planning_epoch(self, user_id: str) -> int:
        epoch = self._store._bump_epoch_locked(self._conn, user_id)
        self.epoch_bumped = True
        return epoch

    def remember_idempotent(self, key: str, response: dict[str, Any]) -> None:
        existing = self._conn.execute(
            "SELECT 1 FROM idempotency WHERE user_id=? AND key=?", (self.user_id, key)
        ).fetchone()
        if existing is not None:
            raise MetisError(
                ErrorCode.IDEMPOTENCY_MISMATCH,
                "a concurrent request already committed this Idempotency-Key",
            )
        self._store._remember_idempotent_locked(self._conn, self.user_id, key, response)

    def claim(self, claim_key: str, owner: str) -> bool:
        """Conditional single-winner claim, e.g. one notification per occurrence."""
        return self._store._claim_locked(self._conn, self.user_id, claim_key, owner)


class SqliteStateStore:
    """Local authoritative state. Safe for the API process plus a worker thread."""

    mode = AdapterMode.LOCAL

    def __init__(self, path: str | None = None, clock: Any | None = None) -> None:
        cfg = settings()
        self._path = Path(path or cfg.sqlite_path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._apply_schema()

    def _apply_schema(self) -> None:
        """Several worker processes may open a fresh file at once. Switching it
        to WAL can report "locked" without waiting on the busy handler, so retry
        briefly rather than crash a sibling's startup."""
        for attempt in range(50):
            try:
                self._conn.executescript(SCHEMA)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == 49:
                    raise
                time.sleep(0.1)

    # ------------------------------------------------------------------ #
    # transaction
    # ------------------------------------------------------------------ #

    @contextmanager
    def transaction(self, user_id: str) -> Iterator[_Tx]:
        """One unit of work. Rolls back entirely on any error, so an entity
        change can never be committed without its outbox event."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            tx = _Tx(self, self._conn, user_id)
            try:
                yield tx
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ #
    # writes
    # ------------------------------------------------------------------ #

    def _put_locked[R: Record](
        self, conn: sqlite3.Connection, kind: str, record: R, expected_revision: int | None
    ) -> R:
        if kind not in ENTITY_MODELS:
            raise MetisError(ErrorCode.VALIDATION_FAILED, f"unknown record kind {kind!r}")
        now = self._clock.now()
        row = conn.execute(
            "SELECT revision, body, tombstoned FROM records WHERE user_id=? AND kind=? AND id=?",
            (record.user_id, kind, record.id),
        ).fetchone()

        if row is None:
            if expected_revision not in (None, 0):
                raise MetisError(
                    ErrorCode.NOT_FOUND, f"{kind} {record.id} does not exist", current_revision=None
                )
            new_revision = 1
            previous: dict[str, Any] = {}
            created_at = record.created_at or now
        else:
            if row["tombstoned"]:
                raise MetisError(ErrorCode.TOMBSTONED, f"{kind} {record.id} was deleted")
            current = int(row["revision"])
            if expected_revision is None:
                raise MetisError(
                    ErrorCode.REVISION_CONFLICT,
                    f"{kind} {record.id} already exists at revision {current}",
                    current_revision=current,
                )
            if expected_revision != current:
                raise MetisError(
                    ErrorCode.REVISION_CONFLICT,
                    f"{kind} {record.id} is at revision {current}, not {expected_revision}",
                    current_revision=current,
                )
            new_revision = current + 1
            previous = json.loads(row["body"])
            created_at = datetime.fromisoformat(previous["created_at"])

        stored = record.model_copy(
            update={"revision": new_revision, "created_at": created_at, "updated_at": now}
        )
        body = stored.model_dump(mode="json")
        changed = sorted(k for k, v in body.items() if previous.get(k) != v) if previous else sorted(body)

        cols = {
            "state": getattr(stored, "state", None)
            or getattr(stored, "status", None)
            or getattr(stored, "disposition", None),
            "project_id": getattr(stored, "project_id", None),
            "local_date": _iso(getattr(stored, "local_date", None)),
            "deadline_date": _iso(getattr(stored, "deadline_local_date", None)),
            "trigger_at": _iso(getattr(stored, "trigger_at", None) or getattr(stored, "next_sync_at", None)),
        }
        allowed = _QUERY_COLUMNS.get(kind, ())
        cols = {
            k: (str(v) if v is not None else None)
            for k, v in cols.items()
            if k in allowed or (k == "state" and "state" in allowed)
        }

        conn.execute(
            """
            INSERT INTO records (user_id, kind, id, revision, created_at, updated_at, body,
                                 state, project_id, local_date, deadline_date, trigger_at)
            VALUES (:user_id, :kind, :id, :revision, :created_at, :updated_at, :body,
                    :state, :project_id, :local_date, :deadline_date, :trigger_at)
            ON CONFLICT(user_id, kind, id) DO UPDATE SET
                revision=:revision, updated_at=:updated_at, body=:body,
                state=:state, project_id=:project_id, local_date=:local_date,
                deadline_date=:deadline_date, trigger_at=:trigger_at
            """,
            {
                "user_id": stored.user_id,
                "kind": kind,
                "id": stored.id,
                "revision": new_revision,
                "created_at": created_at.isoformat(),
                "updated_at": now.isoformat(),
                "body": json.dumps(body, sort_keys=True),
                "state": cols.get("state"),
                "project_id": cols.get("project_id"),
                "local_date": cols.get("local_date"),
                "deadline_date": cols.get("deadline_date"),
                "trigger_at": cols.get("trigger_at"),
            },
        )
        conn.execute(
            """INSERT INTO revisions (user_id, kind, id, revision, recorded_at, changed_fields, body)
               VALUES (?,?,?,?,?,?,?)""",
            (
                stored.user_id,
                kind,
                stored.id,
                new_revision,
                now.isoformat(),
                json.dumps(changed),
                json.dumps(body, sort_keys=True),
            ),
        )
        self._append_change_locked(conn, stored.user_id, kind, stored.id, new_revision, tombstoned=False)
        return stored

    def _append_change_locked(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        kind: str,
        entity_id: str,
        revision: int | None,
        *,
        tombstoned: bool,
    ) -> None:
        if kind in CHANGE_FEED_SKIP_KINDS:
            return
        conn.execute(
            """INSERT INTO changes (user_id, kind, id, revision, tombstoned, recorded_at)
               VALUES (?,?,?,?,?,?)""",
            (user_id, kind, entity_id, revision, int(tombstoned), self._clock.now().isoformat()),
        )

    def _append_outbox_locked(self, conn: sqlite3.Connection, event: OutboxEvent) -> None:
        body = event.model_dump(mode="json")
        # Same (user, event_type, idempotency_key) already queued: the caller
        # retried. One logical side effect is correct.
        with suppress(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO outbox (user_id, id, event_type, event_version, target_id,
                       target_revision, correlation_id, idempotency_key, payload, state,
                       attempts, available_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.user_id,
                    event.id,
                    event.event_type,
                    event.event_version,
                    event.target_id,
                    event.target_revision,
                    event.correlation_id,
                    event.idempotency_key,
                    json.dumps(body["payload"], sort_keys=True),
                    event.state.value if hasattr(event.state, "value") else str(event.state),
                    event.attempts,
                    event.available_at.isoformat(),
                    body["created_at"],
                    body["updated_at"],
                ),
            )

    def _bump_epoch_locked(self, conn: sqlite3.Connection, user_id: str) -> int:
        now = self._clock.now().isoformat()
        conn.execute(
            """INSERT INTO user_meta (user_id, planning_epoch, created_at, updated_at)
               VALUES (?, 1, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   planning_epoch = planning_epoch + 1, updated_at = excluded.updated_at""",
            (user_id, now, now),
        )
        row = conn.execute("SELECT planning_epoch FROM user_meta WHERE user_id=?", (user_id,)).fetchone()
        return int(row["planning_epoch"])

    def _remember_idempotent_locked(
        self, conn: sqlite3.Connection, user_id: str, key: str, response: dict[str, Any]
    ) -> None:
        conn.execute(
            """INSERT INTO idempotency (user_id, key, request_hash, response, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id, key) DO NOTHING""",
            (user_id, key, "", json.dumps(response, sort_keys=True), self._clock.now().isoformat()),
        )

    def _claim_locked(self, conn: sqlite3.Connection, user_id: str, claim_key: str, owner: str) -> bool:
        try:
            conn.execute(
                "INSERT INTO claims (claim_key, user_id, owner, claimed_at) VALUES (?,?,?,?)",
                (claim_key, user_id, owner, self._clock.now().isoformat()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #

    def _hydrate(self, kind: str, row: sqlite3.Row) -> Any:
        return ENTITY_MODELS[kind].model_validate(json.loads(row["body"]))

    def get(self, user_id: str, kind: str, entity_id: str) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body, tombstoned FROM records WHERE user_id=? AND kind=? AND id=?",
                (user_id, kind, entity_id),
            ).fetchone()
        if row is None or row["tombstoned"]:
            return None
        return self._hydrate(kind, row)

    def _page(
        self,
        kind: str,
        user_id: str,
        where: str,
        params: Sequence[Any],
        *,
        cursor: str | None,
        limit: int,
        order: str = "id",
    ) -> Page[Any]:
        bounded = max(1, min(limit, 500))
        sql = (
            f"SELECT id, body FROM records WHERE user_id=? AND kind=? AND tombstoned=0 {where} "
            f"AND id > ? ORDER BY {order}, id LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (user_id, kind, *params, cursor or "", bounded + 1)).fetchall()
        truncated = len(rows) > bounded
        rows = rows[:bounded]
        items = [self._hydrate(kind, r) for r in rows]
        # `complete` is what stops the planner claiming full coverage after a
        # truncated scan.
        return Page(
            items=items, next_cursor=rows[-1]["id"] if truncated and rows else None, complete=not truncated
        )

    def list_tasks(
        self,
        user_id: str,
        *,
        states: Sequence[TaskState] | None = None,
        project_id: str | None = None,
        due_before: date | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[Task]:
        clauses: list[str] = []
        params: list[Any] = []
        if states:
            marks = ",".join("?" for _ in states)
            clauses.append(f"AND state IN ({marks})")
            params.extend(s.value if hasattr(s, "value") else str(s) for s in states)
        if project_id:
            clauses.append("AND project_id = ?")
            params.append(project_id)
        if due_before:
            clauses.append("AND deadline_date IS NOT NULL AND deadline_date <= ?")
            params.append(due_before.isoformat())
        return self._page("task", user_id, " ".join(clauses), params, cursor=cursor, limit=limit)

    def list_events(
        self,
        user_id: str,
        *,
        local_date: date | None = None,
        until: date | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[Event]:
        clauses: list[str] = []
        params: list[Any] = []
        if local_date and until:
            clauses.append("AND local_date IS NOT NULL AND local_date BETWEEN ? AND ?")
            params.extend([local_date.isoformat(), until.isoformat()])
        elif local_date:
            clauses.append("AND local_date = ?")
            params.append(local_date.isoformat())
        return self._page("event", user_id, " ".join(clauses), params, cursor=cursor, limit=limit)

    def list_jobs(
        self, user_id: str, *, state: JobState | None = None, cursor: str | None = None, limit: int = 100
    ) -> Page[Job]:
        where, params = ("AND state = ?", [JobState(state).value]) if state else ("", [])
        return self._page("job", user_id, where, params, cursor=cursor, limit=limit)

    def find_source_by_client_id(self, user_id: str, client_capture_id: str) -> SourceArtifact | None:
        page = self._page(
            "source",
            user_id,
            "AND json_extract(body, '$.client_capture_id') = ?",
            [client_capture_id],
            cursor=None,
            limit=1,
        )
        return page.items[0] if page.items else None

    def list_proposals(
        self, user_id: str, *, source_id: str | None = None, cursor: str | None = None, limit: int = 100
    ) -> Page[ExtractionProposal]:
        where, params = ("AND json_extract(body, '$.source_id') = ?", [source_id]) if source_id else ("", [])
        return self._page("proposal", user_id, where, params, cursor=cursor, limit=limit)

    def list_plans(
        self, user_id: str, *, status: PlanStatus | None = None, cursor: str | None = None, limit: int = 100
    ) -> Page[Plan]:
        where, params = ("AND state = ?", [PlanStatus(status).value]) if status else ("", [])
        return self._page("plan", user_id, where, params, cursor=cursor, limit=limit)

    def list_availability(self, user_id: str, local_date: date) -> list[AvailabilityBlock]:
        page = self._page(
            "availability", user_id, "AND local_date = ?", [local_date.isoformat()], cursor=None, limit=200
        )
        return list(page.items)

    def list_notifications(
        self, user_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> Page[Notification]:
        return self._page("notification", user_id, "", [], cursor=cursor, limit=limit)

    def list_projects(self, user_id: str, *, cursor: str | None = None, limit: int = 100) -> Page[Any]:
        return self._page("project", user_id, "", [], cursor=cursor, limit=limit)

    def list_push_subscriptions(self, user_id: str) -> list[Any]:
        page = self._page("push_subscription", user_id, "", [], cursor=None, limit=50)
        return list(page.items)

    def list_memories(
        self, user_id: str, *, project_id: str | None = None, cursor: str | None = None, limit: int = 100
    ) -> Page[Any]:
        where, params = ("AND project_id = ?", [project_id]) if project_id else ("", [])
        return self._page("memory", user_id, where, params, cursor=cursor, limit=limit)

    def list_sources(
        self, user_id: str, *, cursor: str | None = None, limit: int = 100
    ) -> Page[SourceArtifact]:
        return self._page("source", user_id, "", [], cursor=cursor, limit=limit)

    def list_connectors(self, user_id: str, *, cursor: str | None = None, limit: int = 100) -> Page[Any]:
        return self._page("connector", user_id, "", [], cursor=cursor, limit=limit)

    def due_connector_ids(self, *, now: datetime, limit: int = 50) -> list[str]:
        """Active connectors whose ``next_sync_at`` has passed, as ``user_id/id``."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT user_id, id FROM records
                   WHERE kind='connector' AND tombstoned=0 AND state='active'
                     AND (trigger_at IS NULL OR trigger_at <= ?)
                   ORDER BY trigger_at LIMIT ?""",
                (now.isoformat(), limit),
            ).fetchall()
        return [f"{r['user_id']}/{r['id']}" for r in rows]

    def list_reminders(
        self, user_id: str, *, state: ReminderState | None = None, cursor: str | None = None, limit: int = 100
    ) -> Page[Reminder]:
        where, params = ("AND state = ?", [ReminderState(state).value]) if state else ("", [])
        return self._page("reminder", user_id, where, params, cursor=cursor, limit=limit)

    def list_reminders_for_target(self, user_id: str, target_id: str) -> list[Reminder]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT body FROM records WHERE user_id=? AND kind='reminder' AND tombstoned=0",
                (user_id,),
            ).fetchall()
        reminders = [self._hydrate("reminder", r) for r in rows]
        return [r for r in reminders if r.target_id == target_id]

    def get_preferences(self, user_id: str) -> Preferences:
        existing = self.get(user_id, "preferences", f"pref_{user_id}")
        if isinstance(existing, Preferences):
            return existing
        now = self._clock.now()
        # Editable defaults — availability is never invented from note wording.
        return Preferences(
            id=f"pref_{user_id}",
            user_id=user_id,
            created_at=now,
            updated_at=now,
            timezone=settings().default_timezone,
        )

    def get_planning_epoch(self, user_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT planning_epoch FROM user_meta WHERE user_id=?", (user_id,)
            ).fetchone()
        return int(row["planning_epoch"]) if row else 0

    def get_revisions(self, user_id: str, kind: str, entity_id: str) -> list[dict[str, Any]]:
        """Source-linked change history, newest first."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT revision, recorded_at, changed_fields FROM revisions
                   WHERE user_id=? AND kind=? AND id=? ORDER BY revision DESC""",
                (user_id, kind, entity_id),
            ).fetchall()
        return [
            {
                "revision": r["revision"],
                "recorded_at": r["recorded_at"],
                "changed_fields": json.loads(r["changed_fields"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # outbox dispatcher support
    # ------------------------------------------------------------------ #

    def claim_outbox(
        self,
        *,
        owner: str,
        limit: int,
        lease_seconds: int,
        lane: Lane = "all",
        max_in_flight: int | None = None,
    ) -> list[OutboxEvent]:
        """Lease pending events. An expired lease is reclaimable, which is how
        a crashed dispatcher's work is recovered.

        ``lane`` restricts the claim to ingest or interactive event types.
        ``max_in_flight`` caps live leases across *all* workers of that lane:
        the count and the claim share one ``BEGIN IMMEDIATE``, so two processes
        cannot both see the same headroom.
        """
        now = self._clock.now()
        cutoff = now.isoformat()
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        lane_sql, lane_params = _lane_filter(lane)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                bounded = max(1, min(limit, 100))
                if max_in_flight is not None:
                    (leased,) = self._conn.execute(
                        f"""SELECT COUNT(*) FROM outbox
                            WHERE state='leased' AND lease_expires_at > ? {lane_sql}""",
                        (cutoff, *lane_params),
                    ).fetchone()
                    bounded = min(bounded, max(0, max_in_flight - int(leased)))
                rows = (
                    self._conn.execute(
                        f"""SELECT * FROM outbox
                            WHERE state IN ('pending','failed','leased')
                              AND available_at <= ?
                              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                              {lane_sql}
                            ORDER BY available_at LIMIT ?""",
                        (cutoff, cutoff, *lane_params, bounded),
                    ).fetchall()
                    if bounded
                    else []
                )
                claimed: list[OutboxEvent] = []
                for row in rows:
                    self._conn.execute(
                        """UPDATE outbox SET state='leased', lease_owner=?, lease_expires_at=?,
                               attempts=attempts+1, updated_at=?
                           WHERE user_id=? AND id=? AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
                        (owner, expires, cutoff, row["user_id"], row["id"], cutoff),
                    )
                    claimed.append(self._outbox_from_row(row, attempts=int(row["attempts"]) + 1))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return claimed

    def _outbox_from_row(self, row: sqlite3.Row, *, attempts: int | None = None) -> OutboxEvent:
        return OutboxEvent(
            id=row["id"],
            user_id=row["user_id"],
            revision=1,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            event_type=row["event_type"],
            event_version=int(row["event_version"]),
            target_id=row["target_id"],
            target_revision=row["target_revision"],
            correlation_id=row["correlation_id"],
            idempotency_key=row["idempotency_key"],
            payload=json.loads(row["payload"]),
            state=row["state"],
            attempts=attempts if attempts is not None else int(row["attempts"]),
            available_at=datetime.fromisoformat(row["available_at"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=datetime.fromisoformat(row["lease_expires_at"])
            if row["lease_expires_at"]
            else None,
            failure=row["failure"],
            error_message=row["error_message"],
        )

    def complete_outbox(
        self,
        event_id: str,
        *,
        user_id: str,
        state: str,
        failure: str | None = None,
        error: str | None = None,
        retry_in_seconds: int | None = None,
    ) -> None:
        """Mark a leased event done / failed / dead-lettered / superseded.

        ``failure`` is a FailureCategory value (it drives retry policy);
        ``error`` is the human-readable detail shown to the user. Keeping them
        in separate columns is what lets the category stay a closed enum.
        """
        now = self._clock.now()
        available_at = (now + timedelta(seconds=retry_in_seconds or 0)).isoformat()
        with self._lock:
            self._conn.execute(
                """UPDATE outbox SET state=?, failure=?, error_message=?, lease_owner=NULL,
                       lease_expires_at=NULL, available_at=?, updated_at=?
                   WHERE user_id=? AND id=?""",
                (state, failure, error, available_at, now.isoformat(), user_id, event_id),
            )
            if state == "done":
                self._resolve_dead_letters_locked(user_id, event_id, now.isoformat())
            elif state == "dead_letter":
                self.evict_dead_letters()

    def _resolve_dead_letters_locked(self, user_id: str, event_id: str, now: str) -> None:
        """A later success on the same target makes its older dead letters moot.
        A purge resolves every event type: the target no longer exists."""
        self._conn.execute(
            """UPDATE outbox SET state='superseded', failure='superseded', updated_at=?
               WHERE user_id=? AND state='dead_letter' AND id<>? AND target_id IS NOT NULL
                 AND EXISTS (
                     SELECT 1 FROM outbox AS done
                     WHERE done.user_id=outbox.user_id AND done.id=?
                       AND done.target_id=outbox.target_id
                       AND done.created_at>=outbox.created_at
                       AND (done.event_type=outbox.event_type OR done.event_type=?)
                 )""",
            (now, user_id, event_id, event_id, EventType.CORPUS_PURGE_REQUESTED),
        )

    def evict_dead_letters(self) -> int:
        """FIFO retention: expire letters past their age, then the oldest over
        each owner's cap. Rows stay for audit but leave the queue."""
        limits = settings().limits
        now = self._clock.now()
        cutoff = (now - timedelta(days=limits.dead_letter_retention_days)).isoformat()
        with self._lock:
            aged = self._conn.execute(
                "UPDATE outbox SET state='expired', updated_at=? WHERE state='dead_letter' AND updated_at<?",
                (now.isoformat(), cutoff),
            ).rowcount
            over = self._conn.execute(
                """UPDATE outbox SET state='expired', updated_at=?
                   WHERE state='dead_letter' AND (user_id, id) IN (
                       SELECT user_id, id FROM (
                           SELECT user_id, id, ROW_NUMBER() OVER (
                               PARTITION BY user_id ORDER BY updated_at DESC, id DESC
                           ) AS position
                           FROM outbox WHERE state='dead_letter'
                       ) WHERE position>?
                   )""",
                (now.isoformat(), limits.dead_letter_max_per_user),
            ).rowcount
        return int(aged) + int(over)

    def list_dead_letters(self, limit: int = 50, *, user_id: str | None = None) -> list[OutboxEvent]:
        """Dead-lettered events, oldest first (FIFO). ``user_id=None`` is the operator view."""
        owner_sql, params = ("AND user_id=?", (user_id,)) if user_id is not None else ("", ())
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM outbox WHERE state='dead_letter' {owner_sql} ORDER BY updated_at, id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._outbox_from_row(r) for r in rows]

    def requeue_outbox(self, user_id: str, event_id: str) -> bool:
        """Send one of the owner's dead letters back to the queue with a fresh
        retry budget. False when it is not (or no longer) dead-lettered."""
        now = self._clock.now().isoformat()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE outbox SET state='pending', attempts=0, failure=NULL, error_message=NULL,
                       lease_owner=NULL, lease_expires_at=NULL, available_at=?, updated_at=?
                   WHERE user_id=? AND id=? AND state='dead_letter'""",
                (now, now, user_id, event_id),
            )
        return cur.rowcount == 1

    def queue_stats(self, user_id: str | None = None) -> QueueStats:
        """Per-lane counts. A lease that has expired is reclaimable, so it is
        reported as pending, not as in flight."""
        now = self._clock.now().isoformat()
        owner_sql, owner_params = ("AND user_id=?", (user_id,)) if user_id is not None else ("", ())
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT {_LANE_SQL} AS lane,
                        CASE WHEN state='leased' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                             THEN 'pending' ELSE state END AS bucket,
                        COUNT(*) AS n
                    FROM outbox
                    WHERE state IN ('pending','leased','failed','dead_letter') {owner_sql}
                    GROUP BY lane, bucket""",
                (*_INGEST_TYPES, now, *owner_params),
            ).fetchall()
        counts: dict[str, dict[str, int]] = {"interactive": {}, "ingest": {}}
        for row in rows:
            counts[row["lane"]][row["bucket"]] = int(row["n"])
        return QueueStats(
            lanes=[
                LaneStats(lane="interactive", **counts["interactive"]),
                LaneStats(lane="ingest", **counts["ingest"]),
            ]
        )

    # ------------------------------------------------------------------ #
    # change feed
    # ------------------------------------------------------------------ #

    def list_changes(self, user_id: str, *, after: int, limit: int = 500) -> tuple[list[Change], int]:
        """Changes with ``seq > after``, oldest first, and the cursor to pass
        next time: the last seq returned, or the current max when none."""
        bounded = max(1, min(limit, 500))
        with self._lock:
            rows = self._conn.execute(
                """SELECT seq, kind, id, revision, tombstoned FROM changes
                   WHERE user_id=? AND seq > ? ORDER BY seq LIMIT ?""",
                (user_id, after, bounded),
            ).fetchall()
        changes = [
            Change(
                seq=int(r["seq"]),
                kind=r["kind"],
                id=r["id"],
                revision=r["revision"],
                tombstoned=bool(r["tombstoned"]),
            )
            for r in rows
        ]
        cursor = changes[-1].seq if changes else max(after, self.max_change_seq(user_id))
        return changes, cursor

    def max_change_seq(self, user_id: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(seq) FROM changes WHERE user_id=?", (user_id,)).fetchone()
        return int(row[0] or 0)

    # ------------------------------------------------------------------ #
    # jobs
    # ------------------------------------------------------------------ #

    def claim_job(self, job_id: str, *, user_id: str, owner: str, lease_seconds: int) -> Job | None:
        """Lease a job. Returns None when another worker holds a live lease."""
        now = self._clock.now()
        job = self.get(user_id, "job", job_id)
        if not isinstance(job, Job):
            return None
        if job.lease_expires_at and job.lease_expires_at > now and job.lease_owner != owner:
            return None
        leased = job.model_copy(
            update={"lease_owner": owner, "lease_expires_at": now + timedelta(seconds=lease_seconds)}
        )
        with self.transaction(user_id) as tx:
            return tx.put("job", leased, expected_revision=job.revision)

    def set_job_state(
        self,
        job_id: str,
        *,
        user_id: str,
        state: JobState,
        failure: str | None = None,
        error_message: str | None = None,
        result_refs: dict[str, str] | None = None,
        metrics: dict[str, float] | None = None,
    ) -> Job:
        job = self.get(user_id, "job", job_id)
        if not isinstance(job, Job):
            raise MetisError(ErrorCode.NOT_FOUND, f"job {job_id} not found")
        now = self._clock.now()
        update: dict[str, Any] = {"state": state}
        if failure:
            update["failure"] = failure
        if error_message:
            update["error_message"] = error_message[:1000]
        if result_refs:
            update["result_refs"] = {**job.result_refs, **result_refs}
        if metrics:
            update["metrics"] = {**job.metrics, **metrics}
        if state in (JobState.READY, JobState.FAILED, JobState.CANCELED):
            update["finished_at"] = now
            update["lease_owner"] = None
            update["lease_expires_at"] = None
        elif job.started_at is None:
            update["started_at"] = now
        with self.transaction(user_id) as tx:
            return tx.put("job", job.model_copy(update=update), expected_revision=job.revision)

    # ------------------------------------------------------------------ #
    # idempotency
    # ------------------------------------------------------------------ #

    def remember_idempotent(self, user_id: str, key: str, response: dict[str, Any]) -> None:
        with self._lock:
            self._remember_idempotent_locked(self._conn, user_id, key, response)

    def recall_idempotent(self, user_id: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT response FROM idempotency WHERE user_id=? AND key=?", (user_id, key)
            ).fetchone()
        return json.loads(row["response"]) if row else None

    def claim_once(self, user_id: str, claim_key: str, owner: str) -> bool:
        """Standalone conditional claim, e.g. one notification per occurrence."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                won = self._claim_locked(self._conn, user_id, claim_key, owner)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return won

    # ------------------------------------------------------------------ #
    # budget ledger
    # ------------------------------------------------------------------ #

    def reserve_budget(
        self, user_id: str, *, local_date: date, micro_usd: int, operation: str, correlation_id: str
    ) -> str:
        """Atomically reserve a conservative upper bound before a paid call.

        The comparison uses reserved + settled, so two concurrent workers
        cannot both pass on the same remaining budget.
        """
        cap = settings().limits.daily_micro_usd
        now = self._clock.now()
        reservation_id = new_id("bres")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO usage_day (user_id, local_date, updated_at) VALUES (?,?,?)
                       ON CONFLICT(user_id, local_date) DO NOTHING""",
                    (user_id, local_date.isoformat(), now.isoformat()),
                )
                row = self._conn.execute(
                    "SELECT reserved_micro_usd, settled_micro_usd FROM usage_day WHERE user_id=? AND local_date=?",
                    (user_id, local_date.isoformat()),
                ).fetchone()
                committed = int(row["reserved_micro_usd"]) + int(row["settled_micro_usd"])
                if committed + micro_usd > cap:
                    self._conn.execute("ROLLBACK")
                    raise MetisError(
                        ErrorCode.BUDGET_EXCEEDED,
                        f"daily budget would be exceeded: {committed} + {micro_usd} > {cap}",
                        details={"operation": operation, "cap_micro_usd": str(cap)},
                    )
                self._conn.execute(
                    """UPDATE usage_day SET reserved_micro_usd = reserved_micro_usd + ?,
                           model_calls = model_calls + 1, updated_at = ?
                       WHERE user_id=? AND local_date=?""",
                    (micro_usd, now.isoformat(), user_id, local_date.isoformat()),
                )
                self._conn.execute(
                    """INSERT INTO budget_reservations (id, user_id, local_date, operation,
                           reserved_micro_usd, state, correlation_id, created_at)
                       VALUES (?,?,?,?,?,'reserved',?,?)""",
                    (
                        reservation_id,
                        user_id,
                        local_date.isoformat(),
                        operation,
                        micro_usd,
                        correlation_id,
                        now.isoformat(),
                    ),
                )
                self._conn.execute("COMMIT")
            except MetisError:
                raise
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return reservation_id

    def settle_budget(
        self,
        reservation_id: str,
        *,
        user_id: str,
        actual_micro_usd: int | None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        uncertain: bool = False,
    ) -> None:
        """Settle a reservation against actual usage.

        ``uncertain=True`` keeps the conservative reservation charged: a request
        that timed out may still have been billed, so the hold is retained
        until authoritative usage arrives.
        """
        now = self._clock.now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM budget_reservations WHERE id=? AND user_id=?",
                    (reservation_id, user_id),
                ).fetchone()
                if row is None or row["state"] != "reserved":
                    self._conn.execute("ROLLBACK")
                    return  # idempotent: already settled or unknown
                reserved = int(row["reserved_micro_usd"])
                if uncertain:
                    state, charge = "retained_uncertain", reserved
                elif actual_micro_usd is None:
                    state, charge = "released", 0
                else:
                    state, charge = "settled", actual_micro_usd
                self._conn.execute(
                    """UPDATE budget_reservations SET state=?, actual_micro_usd=?, input_tokens=?,
                           output_tokens=?, settled_at=? WHERE id=?""",
                    (state, charge, input_tokens, output_tokens, now.isoformat(), reservation_id),
                )
                self._conn.execute(
                    """UPDATE usage_day SET reserved_micro_usd = MAX(0, reserved_micro_usd - ?),
                           settled_micro_usd = settled_micro_usd + ?, updated_at=?
                       WHERE user_id=? AND local_date=?""",
                    (reserved, charge, now.isoformat(), user_id, row["local_date"]),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def record_speech_usage(self, user_id: str, *, local_date: date, audio_seconds: float) -> None:
        now = self._clock.now().isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO usage_day (user_id, local_date, audio_seconds, speech_jobs, updated_at)
                   VALUES (?,?,?,1,?)
                   ON CONFLICT(user_id, local_date) DO UPDATE SET
                       audio_seconds = audio_seconds + excluded.audio_seconds,
                       speech_jobs = speech_jobs + 1, updated_at = excluded.updated_at""",
                (user_id, local_date.isoformat(), audio_seconds, now),
            )

    def get_usage(self, user_id: str, local_date: date) -> UsageDay:
        now = self._clock.now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM usage_day WHERE user_id=? AND local_date=?",
                (user_id, local_date.isoformat()),
            ).fetchone()
        if row is None:
            return UsageDay(
                id=f"usage_{user_id}_{local_date.isoformat()}",
                user_id=user_id,
                created_at=now,
                updated_at=now,
                local_date=local_date,
            )
        return UsageDay(
            id=f"usage_{user_id}_{local_date.isoformat()}",
            user_id=user_id,
            created_at=now,
            updated_at=datetime.fromisoformat(row["updated_at"]),
            local_date=local_date,
            reserved_micro_usd=int(row["reserved_micro_usd"]),
            settled_micro_usd=int(row["settled_micro_usd"]),
            model_calls=int(row["model_calls"]),
            audio_seconds=float(row["audio_seconds"]),
            speech_jobs=int(row["speech_jobs"]),
        )

    # ------------------------------------------------------------------ #
    # deletion
    # ------------------------------------------------------------------ #

    def tombstone(self, user_id: str, kind: str, entity_id: str) -> None:
        """Remove application access immediately. Object/vector purge is a
        separate resumable job."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    """UPDATE records SET tombstoned=1, updated_at=?
                       WHERE user_id=? AND kind=? AND id=? RETURNING revision""",
                    (self._clock.now().isoformat(), user_id, kind, entity_id),
                )
                row = cur.fetchone()
                cur.fetchall()
                if row is not None:
                    self._append_change_locked(
                        self._conn, user_id, kind, entity_id, int(row[0]), tombstoned=True
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def purge(self, user_id: str, kind: str, entity_id: str) -> None:
        """Hard delete after the async purge job completes."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM records WHERE user_id=? AND kind=? AND id=?", (user_id, kind, entity_id)
            )

    def due_reminder_ids(self, *, now: datetime, limit: int = 50) -> list[str]:
        """Local-mode timer: reminders whose trigger has passed and that are
        still in a schedulable state."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT user_id, id FROM records
                   WHERE kind='reminder' AND tombstoned=0 AND trigger_at IS NOT NULL
                     AND trigger_at <= ? AND state IN ('scheduled','pending_schedule','snoozed')
                   ORDER BY trigger_at LIMIT ?""",
                (now.isoformat(), limit),
            ).fetchall()
        return [f"{r['user_id']}/{r['id']}" for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
