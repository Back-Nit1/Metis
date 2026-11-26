"""Connectors, the queue (lanes + dead letters) and the change feed."""

from __future__ import annotations

import asyncio
import fnmatch
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from metis_contracts import (
    Connector,
    ConnectorKind,
    ConnectorStatus,
    ErrorCode,
    EventType,
    MetisError,
    Mode,
    OutboxEvent,
    new_id,
    settings,
)
from metis_contracts.api import (
    ChangesResponse,
    ConnectorPage,
    CreateConnectorRequest,
    DeadLetter,
    DeadLetterPage,
    JobAccepted,
    QueueStats,
    UpdateConnectorRequest,
)
from metis_contracts.entities import ConnectorConfig
from metis_pipeline.dispatcher import lane_of
from metis_retrieval.connectors import DEFAULT_EXCLUDES, enqueue_sync, remove_connector

from ..deps import Context

router = APIRouter(prefix="/v1", tags=["connectors"])

#: Seconds between cheap ``max_change_seq`` probes while a long-poll waits.
_POLL_SECONDS = 0.5


def _excluded_root(path: Path) -> str | None:
    """The DEFAULT_EXCLUDES pattern a folder root falls under, if any."""
    home = Path.home().resolve()
    for pattern in DEFAULT_EXCLUDES:
        if pattern.startswith("~/"):
            target = home / pattern[2:]
            if path == target or target in path.parents:
                return pattern
        elif any(fnmatch.fnmatch(part.lower(), pattern.lower()) for part in path.parts):
            return pattern
    return None


def _validated_config(config: ConnectorConfig) -> ConnectorConfig:
    """Roots must be absolute, existing directories outside excluded/secret dirs.
    Stored resolved, so a symlinked root cannot later point somewhere else."""
    if not config.paths:
        raise MetisError(ErrorCode.VALIDATION_FAILED, "a local folder connector needs at least one path")
    roots: list[str] = []
    for raw in config.paths:
        path = Path(raw)
        if not path.is_absolute():
            raise MetisError(ErrorCode.VALIDATION_FAILED, f"path must be absolute: {raw}")
        if not path.is_dir():
            raise MetisError(ErrorCode.VALIDATION_FAILED, f"folder not found or not a directory: {raw}")
        resolved = path.resolve()
        if pattern := _excluded_root(resolved):
            raise MetisError(ErrorCode.VALIDATION_FAILED, f"{raw} is inside an excluded folder ({pattern})")
        roots.append(str(resolved))
    return config.model_copy(update={"paths": list(dict.fromkeys(roots))})


def _require_local_folder_access() -> None:
    if settings().mode is Mode.SERVER:
        raise MetisError(
            ErrorCode.FORBIDDEN,
            "local folder connectors are unavailable on the shared server",
        )


@router.get("/connectors", response_model=ConnectorPage)
def list_connectors(
    ctx: Context, cursor: str | None = None, limit: int = Query(default=50, ge=1, le=200)
) -> ConnectorPage:
    return ctx.state.list_connectors(ctx.user_id, cursor=cursor, limit=limit)


@router.post("/connectors", response_model=Connector, status_code=status.HTTP_201_CREATED)
def create_connector(body: CreateConnectorRequest, ctx: Context) -> Connector:
    """Create a local-folder connector and its first full sync in one transaction."""
    _require_local_folder_access()
    ctx.require_idempotency_key()
    if replayed := ctx.replay():
        return Connector.model_validate(replayed)
    config = _validated_config(body.config)
    now = ctx.adapters.clock.now()
    connector = Connector(
        id=new_id("conn"),
        user_id=ctx.user_id,  # ownership is never taken from the body
        created_at=now,
        updated_at=now,
        kind=ConnectorKind.LOCAL_FOLDER,
        title=body.title,
        config=config,
        next_sync_at=now + timedelta(seconds=config.sync_interval_seconds),
    )
    event = OutboxEvent(
        id=new_id("obx"),
        user_id=ctx.user_id,
        created_at=now,
        updated_at=now,
        event_type=EventType.CONNECTOR_SYNC_REQUESTED,
        target_id=connector.id,
        correlation_id=ctx.correlation_id,
        idempotency_key=f"sync:{connector.id}:{now.isoformat()}:1",
        payload={"connector_id": connector.id, "full": True},
        available_at=now,
    )
    with ctx.state.transaction(ctx.user_id) as tx:
        stored = tx.put("connector", connector)
        tx.append_outbox(event)
    ctx.remember(stored.model_dump(mode="json"))
    return stored


@router.get("/connectors/{connector_id}", response_model=Connector)
def get_connector(connector_id: str, ctx: Context) -> Connector:
    return ctx.require("connector", connector_id, Connector)


@router.patch("/connectors/{connector_id}", response_model=Connector)
def patch_connector(connector_id: str, body: UpdateConnectorRequest, ctx: Context) -> Connector:
    connector = ctx.require("connector", connector_id, Connector)
    if connector.kind is ConnectorKind.LOCAL_FOLDER:
        _require_local_folder_access()
    if replayed := ctx.replay():
        return Connector.model_validate(replayed)
    if connector.revision != body.expected_revision:
        raise MetisError(
            ErrorCode.REVISION_CONFLICT,
            f"connector {connector_id} is at revision {connector.revision}",
            current_revision=connector.revision,
        )
    update: dict[str, Any] = {}
    if body.title is not None:
        update["title"] = body.title
    if body.config is not None:
        update["config"] = _validated_config(body.config)
    if body.status is not None:
        if connector.status is ConnectorStatus.NEEDS_AUTH:
            raise MetisError(
                ErrorCode.INVALID_STATE_TRANSITION, "this connector needs re-authorisation before it can run"
            )
        update["status"] = ConnectorStatus(body.status)
    with ctx.state.transaction(ctx.user_id) as tx:
        stored = tx.put(
            "connector", connector.model_copy(update=update), expected_revision=connector.revision
        )
    ctx.remember(stored.model_dump(mode="json"))
    return stored


@router.delete("/connectors/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connector(connector_id: str, ctx: Context) -> Response:
    """Tombstone the connector and its sources; purges run in the background."""
    connector = ctx.require("connector", connector_id, Connector)
    remove_connector(ctx.state, user_id=ctx.user_id, connector=connector, clock=ctx.adapters.clock)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/connectors/{connector_id}/sync", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED
)
def sync_connector_now(connector_id: str, ctx: Context) -> JobAccepted:
    connector = ctx.require("connector", connector_id, Connector)
    if connector.kind is ConnectorKind.LOCAL_FOLDER:
        _require_local_folder_access()
    ctx.require_idempotency_key()
    if replayed := ctx.replay():
        return JobAccepted.model_validate(replayed)
    if connector.status in (ConnectorStatus.PAUSED, ConnectorStatus.NEEDS_AUTH):
        # The worker would skip it; accepting it would be a dishonest 202.
        raise MetisError(
            ErrorCode.INVALID_STATE_TRANSITION, f"connector {connector_id} is {connector.status.value}"
        )
    event_id = enqueue_sync(
        ctx.state,
        user_id=ctx.user_id,
        connector_id=connector_id,
        clock=ctx.adapters.clock,
        correlation_id=ctx.correlation_id,
    )
    result = JobAccepted(job_id=event_id, state="queued")
    ctx.remember(result.model_dump(mode="json"))
    return result


@router.get("/queue", response_model=QueueStats)
def queue(ctx: Context) -> QueueStats:
    return ctx.state.queue_stats(ctx.user_id)


@router.get("/queue/dead-letters", response_model=DeadLetterPage)
def dead_letters(ctx: Context, limit: int = Query(default=50, ge=1, le=200)) -> DeadLetterPage:
    items = []
    for event in ctx.state.list_dead_letters(limit, user_id=ctx.user_id):
        title = None
        if event.target_id:
            source = ctx.state.get(ctx.user_id, "source", event.target_id)
            origin = getattr(source, "origin", None)
            title = origin.title if origin is not None else None
        items.append(
            DeadLetter(
                event_id=event.id,
                event_type=str(event.event_type),
                lane=lane_of(event.event_type),
                target_id=event.target_id,
                attempts=event.attempts,
                failure=getattr(event.failure, "value", event.failure),
                error_message=event.error_message,
                updated_at=event.updated_at,
                title=title,
            )
        )
    return DeadLetterPage(items=items)


@router.post("/queue/dead-letters/{event_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_dead_letter(event_id: str, ctx: Context) -> dict[str, Any]:
    """Requeue once per key: a double-click replays instead of answering 404."""
    ctx.require_idempotency_key()
    if replayed := ctx.replay():
        return replayed
    if not ctx.state.requeue_outbox(ctx.user_id, event_id):
        raise MetisError(ErrorCode.NOT_FOUND, f"dead letter {event_id} not found")
    response = {"event_id": event_id}
    ctx.remember(response)
    return response


@router.get("/changes", response_model=ChangesResponse)
async def changes(
    ctx: Context,
    after: int | None = Query(default=None, ge=0),
    wait: int = Query(default=0, ge=0, le=25),
) -> ChangesResponse:
    """Long-poll change feed. Store calls run in the threadpool and the wait is
    an ``asyncio.sleep``, so a waiting client never blocks other requests."""
    state, user_id = ctx.state, ctx.user_id
    current = await run_in_threadpool(state.max_change_seq, user_id)
    if after is None or after > current:
        # No cursor: skip history. Unknown cursor: the client must refetch.
        stats = await run_in_threadpool(state.queue_stats, user_id)
        return ChangesResponse(cursor=current, reset=after is not None, queue=stats)
    deadline = time.monotonic() + min(wait, settings().limits.changes_wait_seconds)
    while current <= after and time.monotonic() < deadline:
        await asyncio.sleep(_POLL_SECONDS)
        current = await run_in_threadpool(state.max_change_seq, user_id)
    found, cursor = await run_in_threadpool(lambda: state.list_changes(user_id, after=after))
    stats = await run_in_threadpool(state.queue_stats, user_id)
    return ChangesResponse(cursor=cursor, changes=found, queue=stats)
