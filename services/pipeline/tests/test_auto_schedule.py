"""After extraction, only clear, fully-specified reminder tasks go to the scheduler."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from metis_contracts import (
    AdapterMode,
    EventEnvelope,
    EventType,
    ExtractedItem,
    ExtractedItemKind,
    ExtractionProposal,
    FixedClock,
    ProposalDisposition,
    RequestedAction,
    SourceRef,
    new_id,
    reset_settings_cache,
)
from metis_pipeline.dispatcher import DispatchContext
from metis_pipeline.handlers import handle_extraction_completed
from metis_state.sqlite_store import SqliteStateStore
from test_extraction import USER, source

T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)


@pytest.fixture()
def store(tmp_path):
    reset_settings_cache()
    clock = FixedClock(T0)
    s = SqliteStateStore(str(tmp_path / "s.sqlite"), clock=clock)
    with s.transaction(USER) as tx:
        tx.put("source", source())
    yield s
    reset_settings_cache()


def item(**fields) -> ExtractedItem:
    base = {
        "kind": ExtractedItemKind.TASK,
        "title": "Call the dentist",
        "requested_actions": [RequestedAction.REMIND_GENERIC],
        "local_date": date(2026, 10, 6),
        "remind_local_time": "15:00",
        "evidence": [SourceRef(source_id="src_1", source_version=1, span_ref="0:10")],
    }
    return ExtractedItem(**{**base, **fields})


def run(store, items) -> ExtractionProposal:
    with store.transaction(USER) as tx:
        proposal = tx.put(
            "proposal",
            ExtractionProposal(
                id=new_id("prop"),
                user_id=USER,
                created_at=T0,
                updated_at=T0,
                source_id="src_1",
                source_version=1,
                items=items,
                extractor_version="t",
                model_id="t",
                adapter_mode=AdapterMode.FIXTURE,
                prompt_version="t",
            ),
        )
    envelope = EventEnvelope(
        event_id=new_id("evt"),
        event_type=EventType.EXTRACTION_COMPLETED,
        user_id=USER,
        correlation_id="corr_1",
        target_id=proposal.id,
        idempotency_key=f"x:{proposal.id}",
        payload={"proposal_id": proposal.id},
    )
    context = DispatchContext(state_store=store, source_store=None, clock=FixedClock(T0))
    handle_extraction_completed(envelope, context)
    return store.get(USER, "proposal", proposal.id)


def reminders(store, proposal):
    return [r for tid in proposal.created_entity_ids for r in store.list_reminders_for_target(USER, tid)]


def test_clear_reminder_task_is_scheduled_as_pending_schedule(store):
    proposal = run(store, [item()])
    assert proposal.disposition is ProposalDisposition.ACCEPTED
    (reminder,) = reminders(store, proposal)
    assert reminder.state == "pending_schedule", "never claims scheduled before the sync is confirmed"


def test_missing_time_stays_pending_for_review(store):
    proposal = run(store, [item(remind_local_time=None)])
    assert proposal.disposition is ProposalDisposition.PENDING
    assert store.list_tasks(USER).items == []


def test_mixed_proposal_is_never_split(store):
    thought = item(kind=ExtractedItemKind.THOUGHT, title="Maybe try caching", requested_actions=[])
    proposal = run(store, [item(), thought])
    assert proposal.disposition is ProposalDisposition.PENDING
    assert store.list_tasks(USER).items == []
