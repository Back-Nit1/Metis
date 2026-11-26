"""Queue lanes, in-flight cap, dead-letter requeue, queue stats, change feed."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from metis_contracts import EventType, FixedClock, OutboxEvent, Task
from metis_state.sqlite_store import SqliteStateStore

T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
USER = "user_a"
OTHER = "user_b"
INGEST = EventType.SOURCE_INGEST_REQUESTED
INTERACTIVE = EventType.TASK_CHANGED


@pytest.fixture()
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture()
def store(tmp_path, clock) -> SqliteStateStore:
    return SqliteStateStore(str(tmp_path / "state.sqlite"), clock=clock)


def enqueue(store, key: str, event_type: str, user: str = USER) -> str:
    event = OutboxEvent(
        id=f"obx_{key}",
        user_id=user,
        created_at=T0,
        updated_at=T0,
        event_type=event_type,
        correlation_id="corr",
        idempotency_key=key,
        available_at=T0,
    )
    with store.transaction(user) as tx:
        tx.append_outbox(event)
    return event.id


def stats(store, user=None) -> dict[str, dict[str, int]]:
    return {s.lane: s.model_dump(exclude={"lane"}) for s in store.queue_stats(user).lanes}


def test_lanes_split_the_outbox(store):
    enqueue(store, "a", INGEST)
    enqueue(store, "b", INTERACTIVE)
    assert [
        e.event_type for e in store.claim_outbox(owner="i", limit=10, lease_seconds=60, lane="interactive")
    ] == [INTERACTIVE]
    assert [
        e.event_type for e in store.claim_outbox(owner="g", limit=10, lease_seconds=60, lane="ingest")
    ] == [INGEST]


def test_ingest_in_flight_cap_spans_workers(store, clock):
    for i in range(5):
        enqueue(store, f"i{i}", INGEST)
    claim = lambda owner: store.claim_outbox(  # noqa: E731
        owner=owner, limit=10, lease_seconds=60, lane="ingest", max_in_flight=3
    )
    assert len(claim("w1")) == 3
    assert claim("w2") == [], "the cap is global, not per worker"
    store.complete_outbox("obx_i0", user_id=USER, state="done")
    assert len(claim("w2")) == 1
    clock.advance(seconds=61)  # expired leases stop counting against the cap
    assert len(claim("w3")) == 3


def test_queue_stats_and_requeue(store, clock):
    enqueue(store, "dead", INGEST)
    enqueue(store, "live", INTERACTIVE)
    enqueue(store, "other", INGEST, user=OTHER)
    store.claim_outbox(owner="w", limit=10, lease_seconds=60, lane="ingest")
    store.complete_outbox(
        "obx_dead", user_id=USER, state="dead_letter", failure="validation", error="corrupt pdf"
    )
    store.claim_outbox(owner="w", limit=10, lease_seconds=60, lane="interactive")

    assert stats(store, USER) == {
        "interactive": {"pending": 0, "leased": 1, "failed": 0, "dead_letter": 0},
        "ingest": {"pending": 0, "leased": 0, "failed": 0, "dead_letter": 1},
    }
    assert stats(store)["ingest"]["leased"] == 1, "operator view includes every user"
    clock.advance(seconds=61)
    assert stats(store, USER)["interactive"]["pending"] == 1, "an expired lease is pending again"

    assert [e.id for e in store.list_dead_letters(user_id=USER)] == ["obx_dead"]
    assert store.list_dead_letters(user_id=OTHER) == []
    assert store.requeue_outbox(OTHER, "obx_dead") is False, "owner-scoped"
    assert store.requeue_outbox(USER, "obx_live") is False, "only dead letters"
    assert store.requeue_outbox(USER, "obx_dead") is True
    claimed = store.claim_outbox(owner="w", limit=10, lease_seconds=60, lane="ingest", max_in_flight=5)
    (again,) = [e for e in claimed if e.id == "obx_dead"]
    assert again.attempts == 1 and again.failure is None


def test_every_put_and_tombstone_appends_a_change(store):
    task = Task(id="task_1", user_id=USER, created_at=T0, updated_at=T0, title="Register")
    with store.transaction(USER) as tx:
        stored = tx.put("task", task)
        tx.append_outbox(OutboxEvent(**{**_obx(), "id": "obx_x"}))
    with store.transaction(USER) as tx:
        tx.put("task", stored.model_copy(update={"title": "Renamed"}), expected_revision=1)
    store.tombstone(USER, "task", "task_1")
    store.tombstone(USER, "task", "missing")  # nothing changed, nothing recorded

    changes, cursor = store.list_changes(USER, after=0)
    assert [(c.kind, c.id, c.revision, c.tombstoned) for c in changes] == [
        ("task", "task_1", 1, False),
        ("task", "task_1", 2, False),
        ("task", "task_1", 2, True),
    ]
    assert cursor == changes[-1].seq == store.max_change_seq(USER)
    assert store.list_changes(USER, after=cursor) == ([], cursor)
    assert store.list_changes(OTHER, after=0) == ([], 0)
    page, page_cursor = store.list_changes(USER, after=0, limit=1)
    assert len(page) == 1 and page_cursor == page[0].seq


def test_a_rolled_back_write_leaves_no_change(store):
    task = Task(id="task_1", user_id=USER, created_at=T0, updated_at=T0, title="Register")
    with pytest.raises(RuntimeError), store.transaction(USER) as tx:
        tx.put("task", task)
        raise RuntimeError
    assert store.max_change_seq(USER) == 0


def test_an_existing_database_gains_the_changes_table(tmp_path, clock):
    path = tmp_path / "old.sqlite"
    SqliteStateStore(str(path), clock=clock).close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE changes")
    store = SqliteStateStore(str(path), clock=clock)
    with store.transaction(USER) as tx:
        tx.put("task", Task(id="task_t", user_id=USER, created_at=T0, updated_at=T0, title="x"))
    assert store.max_change_seq(USER) == 1


def _obx() -> dict:
    return {
        "user_id": USER,
        "created_at": T0,
        "updated_at": T0,
        "event_type": INTERACTIVE,
        "correlation_id": "c",
        "idempotency_key": "x",
        "available_at": T0,
    }


def test_timer_claims_bypass_older_transcription_backlog(store):
    from metis_contracts import TIMER_EVENT_TYPES

    for i in range(30):
        enqueue(store, f"audio{i}", EventType.CAPTURE_COMPLETED)
    for i, event_type in enumerate(sorted(TIMER_EVENT_TYPES)):
        enqueue(store, f"timer{i}", event_type)
    timers = store.claim_outbox(owner="timers", limit=25, lease_seconds=60, lane="timers")
    assert {event.event_type for event in timers} == TIMER_EVENT_TYPES
    jobs = store.claim_outbox(owner="jobs", limit=50, lease_seconds=60, lane="jobs")
    assert len(jobs) == 30
    assert all(event.event_type == EventType.CAPTURE_COMPLETED for event in jobs)
