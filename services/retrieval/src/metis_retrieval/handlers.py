"""Retrieval handlers: asynchronous indexing and vector purge."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from metis_contracts import ErrorCode, EventEnvelope, EventType, MetisError, settings
from metis_contracts.enums import FailureCategory, SourceLifecycle
from metis_contracts.events import ConnectorSyncPayload, MemoryIndexPayload, PurgePayload, SourceIngestPayload
from metis_pipeline.dispatcher import DispatchContext, HandlerRegistry, classify

from .connectors import _sha256, sync_connector
from .corpus_store import ChunkRow, chunk_id_for
from .embedder import content_hash
from .ingestion import chunk_text, needs_embedding
from .parsers import parse_file

log = logging.getLogger("metis.retrieval.handlers")

registry = HandlerRegistry()


@registry.register(EventType.MEMORY_INDEX_REQUESTED)
def handle_memory_index(envelope: EventEnvelope, context: DispatchContext) -> None:
    """Embed and index one memory, verifying freshness before writing.

    Indexing is asynchronous on purpose: the authoritative task view must stay
    usable before its vector update lands.
    """
    if context.memory_store is None or context.embedder is None:
        raise MetisError(ErrorCode.PROVIDER_UNAVAILABLE, "no memory store or embedder is configured")

    payload = MemoryIndexPayload.model_validate(envelope.payload)
    state = context.state_store
    memory = state.get(envelope.user_id, "memory", payload.memory_id)
    if memory is None:
        raise MetisError(ErrorCode.TOMBSTONED, "the memory was deleted before indexing")

    source = state.get(envelope.user_id, "source", payload.source_id)
    if source is None:
        raise MetisError(ErrorCode.TOMBSTONED, "the source was deleted before indexing")
    if payload.source_version < source.current_version:
        raise MetisError(
            ErrorCode.REVISION_CONFLICT,
            "a corrected transcript supersedes this index request",
        )
    if memory.content_hash != payload.content_hash:
        raise MetisError(ErrorCode.REVISION_CONFLICT, "the memory content changed; a newer request exists")

    known = getattr(context.memory_store, "known_hashes", None)
    known_hashes = known(envelope.user_id, context.embedder.model_id) if known else set()
    if not needs_embedding(memory, known_hashes):
        log.info("reusing the existing embedding for %s", memory.id)
        vectors = None
    else:
        vectors = context.embedder.embed([memory.content])

    if vectors is not None:
        context.memory_store.upsert(envelope.user_id, [memory], vectors)

    with state.transaction(envelope.user_id) as tx:
        tx.put(
            "memory",
            memory.model_copy(
                update={
                    "indexed_at": context.clock.now(),
                    "indexed_source_version": payload.source_version,
                    "embedding_model_id": context.embedder.model_id,
                    "embedding_dimensions": context.embedder.dimensions,
                }
            ),
            expected_revision=memory.revision,
        )


@registry.register(EventType.MEMORY_PURGE_REQUESTED)
def handle_memory_purge(envelope: EventEnvelope, context: DispatchContext) -> None:
    """Remove vectors for deleted memories. Resumable via the payload cursor."""
    if context.memory_store is None:
        return
    payload = PurgePayload.model_validate(envelope.payload)
    context.memory_store.delete(envelope.user_id, [payload.target_id])
    context.state_store.purge(envelope.user_id, "memory", payload.target_id)
    log.info("purged memory %s from the index", payload.target_id)


@registry.register(EventType.CONNECTOR_SYNC_REQUESTED)
def handle_connector_sync(envelope: EventEnvelope, context: DispatchContext) -> None:
    payload = ConnectorSyncPayload.model_validate(envelope.payload)
    raised = sync_connector(
        context.state_store,
        user_id=envelope.user_id,
        connector_id=payload.connector_id,
        clock=context.clock,
        full=payload.full,
        correlation_id=envelope.correlation_id,
    )
    log.info("connector %s sync raised %d events", payload.connector_id, raised)


_ORIGIN = {"file": "file", "email": "email", "drive_file": "drive"}


def _mark(context: DispatchContext, source: Any, **update: Any) -> Any:
    with context.state_store.transaction(source.user_id) as tx:
        return tx.put("source", source.model_copy(update=update), expected_revision=source.revision)


@registry.register(EventType.SOURCE_INGEST_REQUESTED)
def handle_source_ingest(envelope: EventEnvelope, context: DispatchContext) -> None:
    """Parse -> chunk -> index -> embed one connector source.

    Chunks are keyword-searchable as soon as they are written; if the embedding
    model is down the event retries and only the missing vectors are computed.
    Malformed input marks the source failed and re-raises so it dead-letters.
    """
    corpus = getattr(context.memory_store, "corpus", None)
    if corpus is None:
        raise MetisError(ErrorCode.PROVIDER_UNAVAILABLE, "no corpus index is configured")
    payload = SourceIngestPayload.model_validate(envelope.payload)
    user_id = envelope.user_id
    source = context.state_store.get(user_id, "source", payload.source_id)
    if source is None or source.origin is None:
        raise MetisError(ErrorCode.TOMBSTONED, "the source was deleted before ingestion")
    if payload.source_version < source.current_version:
        raise MetisError(ErrorCode.REVISION_CONFLICT, "a newer version of this file supersedes this request")

    path = Path(source.origin.external_id)
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise MetisError(ErrorCode.TOMBSTONED, "the file vanished; the next sync tombstones it") from exc
    limit = settings().limits.corpus_max_file_bytes
    if size > limit:
        _mark(
            context,
            source,
            lifecycle=SourceLifecycle.FAILED,
            failure=FailureCategory.LIMIT_EXCEEDED,
            error_message=f"skipped: {size} bytes exceeds the {limit}-byte corpus limit",
        )
        return

    # Same bytes + version + model = same result, so a redelivered job skips the
    # parse and embed. Only a finished ingest writes a receipt; failures rerun.
    model_id = context.embedder.model_id if context.embedder is not None else ""
    work_key = hashlib.sha256(
        f"{source.id}:{source.current_version}:{_sha256(path)}:{model_id}".encode()
    ).hexdigest()
    done = corpus.ingested_chunk_count(user_id, source.id, work_key)
    if done is not None:
        log.info("source %s already ingested for this content; skipping", source.id)
        if source.lifecycle is not SourceLifecycle.READY:  # crashed after indexing
            _mark(
                context,
                source,
                lifecycle=SourceLifecycle.READY,
                chunk_count=done,
                failure=None,
                error_message=None,
            )
        return

    try:
        text = parse_file(path)
    except MetisError as exc:
        _mark(
            context,
            source,
            lifecycle=SourceLifecycle.FAILED,
            failure=classify(exc),
            error_message=exc.message[:1000],
        )
        if exc.code is ErrorCode.UNSUPPORTED_MEDIA:
            return  # readable later, not broken: a visible skip, not a dead letter
        raise

    chunks = chunk_text(text, title=source.origin.title, path=source.origin.uri)
    rows = [
        ChunkRow(
            chunk_id=chunk_id_for(user_id, source.id, source.current_version, c.start_char),
            user_id=user_id,
            source_id=source.id,
            source_version=source.current_version,
            connector_id=source.origin.connector_id,
            origin=_ORIGIN.get(source.kind.value, "file"),
            title=source.origin.title,
            uri=source.origin.uri,
            start_char=c.start_char,
            end_char=c.end_char,
            content=c.text,
        )
        for c in chunks
    ]
    hashes = [content_hash(c.embed_text) for c in chunks]
    if rows:
        corpus.replace_source(user_id, rows, hashes)
    else:
        corpus.delete_source(user_id, source.id)  # empty file: nothing to find

    embedder = context.embedder
    if embedder is not None and rows:
        embed_text = dict(zip(hashes, (c.embed_text for c in chunks), strict=True))
        pending = corpus.pending_embeddings(user_id, source.id, embedder.model_id)
        reused = corpus.known_vectors(user_id, embedder.model_id, [h for _, h in pending])
        todo = [(cid, h) for cid, h in pending if h not in reused]
        fresh = embedder.embed([embed_text[h] for _, h in todo]) if todo else []
        vectors: dict[str, Any] = {cid: reused[h] for cid, h in pending if h in reused}
        vectors.update({cid: vec for (cid, _), vec in zip(todo, fresh, strict=True)})
        corpus.set_vectors(user_id, embedder.model_id, vectors)
    corpus.record_ingest(user_id, source.id, work_key, len(rows))

    _mark(
        context,
        source,
        lifecycle=SourceLifecycle.READY,
        chunk_count=len(rows),
        failure=None,
        error_message=None,
    )


@registry.register(EventType.CORPUS_PURGE_REQUESTED)
def handle_corpus_purge(envelope: EventEnvelope, context: DispatchContext) -> None:
    """Remove a deleted source's chunks, then its tombstoned record."""
    payload = PurgePayload.model_validate(envelope.payload)
    corpus = getattr(context.memory_store, "corpus", None)
    if corpus is not None:
        removed = corpus.delete_source(envelope.user_id, payload.target_id)
        log.info("purged %d corpus chunks of %s", removed, payload.target_id)
    if context.state_store.get(envelope.user_id, "source", payload.target_id) is None:
        context.state_store.purge(envelope.user_id, "source", payload.target_id)


__all__ = ["registry"]
