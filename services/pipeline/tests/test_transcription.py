"""Inline transcription: one transcript revision and one finalized event per capture."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metis_contracts import (
    EventEnvelope,
    EventType,
    FixedClock,
    JobState,
    ObjectRef,
    SourceKind,
    SourceLifecycle,
    new_id,
    reset_settings_cache,
)
from metis_contracts.entities import SourceArtifact
from metis_pipeline.dispatcher import DispatchContext
from metis_pipeline.transcription import transcribe_inline
from metis_speech import FixtureTranscriber
from metis_state import LocalSourceStore, SqliteStateStore, build_key

T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
USER = "user_a"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("METIS_JOB_MAX_ATTEMPTS", "3")
    reset_settings_cache()
    clock = FixedClock(T0)
    state = SqliteStateStore(str(tmp_path / "s.sqlite"), clock=clock)
    objects = LocalSourceStore(str(tmp_path / "o"), clock=clock)
    ctx = DispatchContext(
        state_store=state,
        source_store=objects,
        clock=clock,
        transcriber=FixtureTranscriber(),
        worker_id="w",
    )
    yield ctx
    reset_settings_cache()


def make_source(ctx, *, user=USER, body=b"audio-bytes"):
    sid = new_id("src")
    ref = ObjectRef(bucket="local", key=build_key(user, sid, ".webm"), content_type="audio/webm")
    ctx.source_store.put_bytes(ref, body, content_type="audio/webm")
    now = ctx.clock.now()
    source = SourceArtifact(
        id=sid,
        user_id=user,
        created_at=now,
        updated_at=now,
        kind=SourceKind.AUDIO,
        lifecycle=SourceLifecycle.PROCESSING,
        capture_timezone="America/New_York",
        captured_at=now,
        object_ref=ref,
    )
    with ctx.state_store.transaction(user) as tx:
        return tx.put("source", source)


def envelope(source, job_id="job_1", user=USER, revision=None):
    return EventEnvelope(
        event_id=new_id("evt"),
        event_type=EventType.CAPTURE_COMPLETED,
        user_id=user,
        correlation_id="corr",
        target_id=source.id,
        target_revision=revision or source.current_version,
        idempotency_key=f"transcribe:{source.id}:{source.current_version}",
        payload={"job_id": job_id},
    )


def job_of(ctx, job_id="job_1", user=USER):
    return ctx.state_store.get(user, "job", job_id)


def finalized_events(ctx):
    events = ctx.state_store.claim_outbox(owner="t", limit=100, lease_seconds=60)
    return [e for e in events if e.event_type == EventType.TRANSCRIPT_FINALIZED]


def test_inline_transcription_commits_one_finalized_revision(env):
    ctx = env
    src = make_source(ctx)
    transcribe_inline(envelope(src), ctx)
    transcribe_inline(envelope(src), ctx)  # duplicate completion is a no-op
    assert job_of(ctx).state is JobState.READY
    assert ctx.state_store.get(USER, "source", src.id).lifecycle is SourceLifecycle.READY
    assert len(finalized_events(ctx)) == 1
