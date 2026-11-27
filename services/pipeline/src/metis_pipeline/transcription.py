"""Transcription execution: inline decode through the configured transcriber.

``capture.completed`` -> ``transcribe_inline`` validates the source, records the
job, decodes the stored bytes and commits exactly one transcript revision and
one ``transcript.finalized``. Uploaded is not transcribed: a capture is READY
only after the commit.
"""

from __future__ import annotations

import logging
from typing import Any

from metis_contracts import (
    TERMINAL_JOB_STATES,
    ErrorCode,
    EventEnvelope,
    EventType,
    JobState,
    MetisError,
    SourceLifecycle,
    TranscriptionJobInput,
    new_id,
    settings,
)
from metis_contracts.entities import (
    Job,
    SourceArtifact,
    SpeechMetadata,
    TranscriptRevision,
    TranscriptSegment,
)
from metis_contracts.events import TranscriptFinalizedPayload

from .dispatcher import DispatchContext, outbox_event

log = logging.getLogger("metis.pipeline.transcription")


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #


def _source_for(envelope: EventEnvelope, context: DispatchContext) -> SourceArtifact:
    state = context.state_store
    source: SourceArtifact | None = state.get(envelope.user_id, "source", envelope.target_id or "")
    if source is None:
        raise MetisError(ErrorCode.TOMBSTONED, "the capture was deleted before transcription")
    if source.current_version != envelope.target_revision:
        raise MetisError(
            ErrorCode.REVISION_CONFLICT,
            f"capture moved to version {source.current_version}; this job is superseded",
        )
    if source.object_ref is None:
        raise MetisError(ErrorCode.VALIDATION_FAILED, "audio capture has no object reference")
    if not context.source_store.owns_key(envelope.user_id, source.object_ref.key):
        raise MetisError(ErrorCode.FORBIDDEN, "object key is outside the owner's prefix")
    return source


def _job_for(
    envelope: EventEnvelope, context: DispatchContext, source: SourceArtifact, state: JobState
) -> Job:
    """Existing job (duplicate completion / retry) or a new one."""
    job_id = str(envelope.payload.get("job_id") or new_id("job"))
    existing: Job | None = context.state_store.get(envelope.user_id, "job", job_id)
    if existing is not None:
        return existing
    now = context.clock.now()
    job = Job(
        id=job_id,
        user_id=envelope.user_id,
        created_at=now,
        updated_at=now,
        job_type="transcribe",
        state=state,
        target_id=source.id,
        target_revision=source.current_version,
        correlation_id=envelope.correlation_id,
        idempotency_key=envelope.idempotency_key,
    )
    with context.state_store.transaction(envelope.user_id) as tx:
        stored: Job = tx.put("job", job)
    return stored


def _job_input(source: SourceArtifact, job: Job) -> TranscriptionJobInput:
    cfg = settings()
    if source.object_ref is None:
        raise MetisError(ErrorCode.VALIDATION_FAILED, "audio capture has no object reference")
    return TranscriptionJobInput(
        recording_id=source.id,
        user_id=job.user_id,
        source_version=source.current_version,
        s3_bucket=source.object_ref.bucket,
        s3_key=source.object_ref.key,
        model_id=cfg.speech.model_id,
        max_audio_seconds=cfg.limits.max_audio_seconds,
        idempotency_key=job.idempotency_key,
        correlation_id=job.correlation_id,
        job_id=job.id,
    )


def commit_transcript(
    context: DispatchContext,
    envelope: EventEnvelope,
    source: SourceArtifact,
    job: Job,
    text: str,
    segments: list[TranscriptSegment],
    meta: dict[str, Any],
) -> None:
    """Persist one transcript revision, one finalized event, and the READY job."""
    state = context.state_store
    state.record_speech_usage(
        envelope.user_id,
        local_date=context.clock.today_local(source.capture_timezone),
        audio_seconds=float(meta.get("audio_seconds", 0.0)),
    )

    # Save the transcript BEFORE extraction: speech errors affect dates, names
    # and intent, so the user must be able to correct it first.
    known = {k: v for k, v in meta.items() if k in SpeechMetadata.model_fields}
    revision = TranscriptRevision(
        source_version=source.current_version,
        text=text,
        asr_text=text,
        edited_by_user=False,
        segments=segments,
        speech=SpeechMetadata.model_validate(known),
        created_at=context.clock.now(),
    )
    updated = source.model_copy(
        update={
            "lifecycle": SourceLifecycle.READY,
            "transcripts": [*source.transcripts, revision],
            "duration_seconds": float(meta.get("audio_seconds", 0.0)) or source.duration_seconds,
        }
    )
    with state.transaction(envelope.user_id) as tx:
        stored = tx.put("source", updated, expected_revision=source.revision)
        if text.strip():
            tx.append_outbox(
                outbox_event(
                    envelope,
                    event_type=EventType.TRANSCRIPT_FINALIZED,
                    target_id=stored.id,
                    target_revision=stored.current_version,
                    payload=TranscriptFinalizedPayload(
                        recording_id=stored.id,
                        source_version=stored.current_version,
                        text_length=len(text),
                    ).model_dump(mode="json"),
                    # Exactly one logical transcript.finalized per accepted
                    # text revision, no matter how many retries happened.
                    idempotency_key=f"transcript:{stored.id}:{stored.current_version}",
                    clock=context.clock,
                )
            )

    state.set_job_state(
        job.id,
        user_id=envelope.user_id,
        state=JobState.READY,
        result_refs={"source_id": source.id, "source_version": str(source.current_version)},
        metrics={
            "audio_seconds": float(meta.get("audio_seconds", 0.0)),
            "decode_seconds": float(meta.get("decode_seconds", 0.0)),
            "model_load_seconds": float(meta.get("model_load_seconds", 0.0) or 0.0),
        },
    )
    if not text.strip():
        log.info("source %s decoded to no speech; nothing downstream was enqueued", source.id)


# --------------------------------------------------------------------------- #
# local / fixture: inline
# --------------------------------------------------------------------------- #


def transcribe_inline(envelope: EventEnvelope, context: DispatchContext) -> None:
    source = _source_for(envelope, context)
    job = _job_for(envelope, context, source, JobState.TRANSCRIBING)
    assert source.object_ref is not None  # _source_for guarantees it
    if job.state in TERMINAL_JOB_STATES:
        return  # duplicate completion; the first run already decided
    if job.state is not JobState.TRANSCRIBING:
        job = context.state_store.set_job_state(job.id, user_id=envelope.user_id, state=JobState.TRANSCRIBING)
    if context.transcriber is None:
        raise MetisError(ErrorCode.PROVIDER_UNAVAILABLE, "no transcriber is configured")

    spec = _job_input(source, job)
    body = context.source_store.get_bytes(source.object_ref)
    text, segments, meta = context.transcriber.transcribe(
        body,
        language=spec.language or settings().speech.language,
        glossary=tuple(spec.glossary),
        max_seconds=spec.max_audio_seconds,
    )
    commit_transcript(context, envelope, source, job, text, segments, meta)
