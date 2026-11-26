"""Local-folder connector: scan roots, upsert file sources, request ingestion.

One sync pass (``CONNECTOR_SYNC_REQUESTED``):

* walk each root (no symlinks), apply ``DEFAULT_EXCLUDES`` then the user's
  globs; secret patterns always win and cannot be re-included
* version = ``mtime_ns:size``; sha256 only when that changes, so a touched but
  identical file is not re-ingested
* new/changed -> upsert ``SourceArtifact(kind=FILE)`` (``current_version``
  bumped on content change) + ``SOURCE_INGEST_REQUESTED`` in one transaction
* missing -> tombstone + ``CORPUS_PURGE_REQUESTED``
* at most ``MAX_EVENTS_PER_PASS`` events per pass; the rest is picked up by a
  re-enqueued continuation pass
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import mimetypes
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from metis_contracts import (
    Connector,
    ConnectorStatus,
    ErrorCode,
    EventType,
    MetisError,
    Mode,
    OutboxEvent,
    SourceArtifact,
    SourceOrigin,
    new_id,
    settings,
)
from metis_contracts.enums import FailureCategory, SourceKind, SourceLifecycle

from .parsers import is_indexable

log = logging.getLogger("metis.retrieval.connectors")

#: Matched (case-insensitively) against every path component. Not overridable.
SECRET_EXCLUDES: tuple[str, ...] = (
    ".ssh",
    ".gnupg",
    ".env*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "Keychains",
)
#: Noise and OS data. ``~/`` entries are home-relative paths, not names, so a
#: project folder called "Library" is still indexed.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    *SECRET_EXCLUDES,
    ".git",
    "node_modules",
    ".venv",
    "__pycache__",
    ".DS_Store",
    "~/Library",
    "AppData",
)
MAX_EVENTS_PER_PASS = 500
_LIST_PAGE = 500


def _excluded(path: Path, rel: str, user_excludes: list[str]) -> bool:
    home = Path.home()
    for pattern in DEFAULT_EXCLUDES:
        if pattern.startswith("~/"):
            target = home / pattern[2:]
            if path == target or target in path.parents:
                return True
        elif any(fnmatch.fnmatch(part.lower(), pattern.lower()) for part in path.parts):
            return True
    return any(
        fnmatch.fnmatch(rel.lower(), g.lower()) or fnmatch.fnmatch(path.name.lower(), g.lower())
        for g in user_excludes
    )


def _included(path: Path, rel: str, includes: list[str]) -> bool:
    if not includes:
        return True
    return any(
        fnmatch.fnmatch(rel.lower(), g.lower()) or fnmatch.fnmatch(path.name.lower(), g.lower())
        for g in includes
    )


def walk(root: Path, *, include_globs: list[str], exclude_globs: list[str]) -> Iterator[Path]:
    """Files under ``root``, sorted, never following symlinks (a link could
    point into ~/.ssh and bypass the excludes)."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not (base / d).is_symlink()
            and not _excluded(base / d, (base / d).relative_to(root).as_posix(), exclude_globs)
        )
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink() or _excluded(path, rel, exclude_globs):
                continue
            # ponytail: reads 8 KiB per file each sync; cache by mtime if huge folders drag.
            if _included(path, rel, include_globs) and is_indexable(path):
                yield path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _event(
    user_id: str, now: datetime, event_type: str, key: str, payload: dict[str, Any], **kw: Any
) -> OutboxEvent:
    return OutboxEvent(
        id=new_id("obx"),
        user_id=user_id,
        created_at=now,
        updated_at=now,
        event_type=event_type,
        correlation_id=kw.pop("correlation_id", None) or new_id("cor"),
        idempotency_key=key,
        payload=payload,
        available_at=now,
        **kw,
    )


def _connector_sources(state: Any, user_id: str, connector_id: str) -> dict[str, SourceArtifact]:
    """Live sources of this connector by external_id.

    ponytail: pages every source of the user; add an origin index to the state
    store when a user holds enough sources for this to show up in a profile.
    """
    found: dict[str, SourceArtifact] = {}
    cursor: str | None = None
    while True:
        page = state.list_sources(user_id, cursor=cursor, limit=_LIST_PAGE)
        for src in page.items:
            if src.origin is not None and src.origin.connector_id == connector_id:
                found[src.origin.external_id] = src
        if not page.next_cursor:
            return found
        cursor = page.next_cursor


def _tombstone_source(tx: Any, src: SourceArtifact, now: datetime, correlation_id: str | None) -> None:
    tx.put(
        "source",
        src.model_copy(update={"lifecycle": SourceLifecycle.TOMBSTONED, "tombstoned_at": now}),
        expected_revision=src.revision,
    )
    tx.append_outbox(
        _event(
            src.user_id,
            now,
            EventType.CORPUS_PURGE_REQUESTED,
            f"purge:{src.id}",
            {"target_id": src.id, "target_kind": "source"},
            target_id=src.id,
            correlation_id=correlation_id,
        )
    )


def enqueue_sync(
    state: Any,
    *,
    user_id: str,
    connector_id: str,
    clock: Any,
    full: bool = False,
    correlation_id: str | None = None,
) -> str:
    """Request one sync pass and push ``next_sync_at`` forward in the same
    transaction, so the scheduler does not enqueue it again meanwhile.
    Returns the outbox event id."""
    now = clock.now()
    connector = state.get(user_id, "connector", connector_id)
    event = _event(
        user_id,
        now,
        EventType.CONNECTOR_SYNC_REQUESTED,
        f"sync:{connector_id}:{now.isoformat()}:{int(full)}",
        {"connector_id": connector_id, "full": full},
        target_id=connector_id,
        correlation_id=correlation_id,
    )
    with state.transaction(user_id) as tx:
        if connector is not None:
            interval = timedelta(seconds=connector.config.sync_interval_seconds)
            tx.put(
                "connector",
                connector.model_copy(update={"next_sync_at": now + interval}),
                expected_revision=connector.revision,
            )
        tx.append_outbox(event)
    return event.id


def enqueue_due_syncs(state: Any, clock: Any) -> int:
    """Called by the interactive worker on its tick. Returns syncs enqueued."""
    if settings().mode is Mode.SERVER:
        return 0  # Desktop filesystem connectors cannot read a shared server's disk.
    count = 0
    for key in state.due_connector_ids(now=clock.now()):
        user_id, connector_id = key.split("/", 1)
        try:
            enqueue_sync(state, user_id=user_id, connector_id=connector_id, clock=clock)
            count += 1
        except Exception as exc:  # one bad connector must not block the rest
            log.warning("could not enqueue sync for %s: %s", key, exc)
    return count


def remove_connector(state: Any, *, user_id: str, connector: Connector, clock: Any) -> int:
    """Tombstone the connector and its sources; one purge event per source.
    Returns the number of sources purged."""
    if connector.user_id != user_id:
        raise ValueError("connector owner mismatch")
    now = clock.now()
    sources = list(_connector_sources(state, user_id, connector.id).values())
    with state.transaction(user_id) as tx:
        for src in sources:
            _tombstone_source(tx, src, now, None)
    for src in sources:
        state.tombstone(user_id, "source", src.id)
    state.tombstone(user_id, "connector", connector.id)
    return len(sources)


def sync_connector(
    state: Any,
    *,
    user_id: str,
    connector_id: str,
    clock: Any,
    full: bool = False,
    correlation_id: str | None = None,
) -> int:
    """One scan pass. Returns the number of events raised."""
    if settings().mode is Mode.SERVER:
        raise MetisError(ErrorCode.FORBIDDEN, "local folder indexing is unavailable on the server")
    connector = state.get(user_id, "connector", connector_id)
    if connector is None or connector.status in (ConnectorStatus.PAUSED, ConnectorStatus.NEEDS_AUTH):
        return 0  # removed or paused since the request; an ERROR connector may heal on a manual sync
    now = clock.now()
    cfg = connector.config

    roots = [Path(p).expanduser() for p in cfg.paths]
    bad = [str(r) for r in roots if not r.is_dir()]
    if bad or not roots:
        message = f"folder not found or not a directory: {', '.join(bad)}" if bad else "no folders configured"
        _finish(state, connector, now, status=ConnectorStatus.ERROR, error=message[:1000])
        return 0

    existing = _connector_sources(state, user_id, connector_id)
    seen: set[str] = set()
    events = 0
    more = False
    max_bytes = settings().limits.corpus_max_file_bytes

    for root in roots:
        for path in walk(root.resolve(), include_globs=cfg.include_globs, exclude_globs=cfg.exclude_globs):
            external_id = str(path)
            seen.add(external_id)
            if more:
                continue  # still record presence, so nothing is wrongly tombstoned
            try:
                st = path.stat()
            except OSError:
                continue
            stamp = f"{st.st_mtime_ns}:{st.st_size}"
            src = existing.get(external_id)
            unchanged = (
                src is not None
                and src.origin is not None
                and src.origin.external_version.startswith(stamp + ":")
            )
            if unchanged and not full:
                continue
            if events >= MAX_EVENTS_PER_PASS:
                more = True
                continue
            if _upsert_file(state, connector, src, path, st, stamp, now, max_bytes, full, correlation_id):
                events += 1

    if not more:
        missing = [src for ext, src in existing.items() if ext not in seen]
        budget = MAX_EVENTS_PER_PASS - events
        for src in missing[:budget]:
            with state.transaction(user_id) as tx:
                _tombstone_source(tx, src, now, correlation_id)
            state.tombstone(user_id, "source", src.id)
        events += min(budget, len(missing))
        more = len(missing) > budget

    _finish(state, connector, now, status=ConnectorStatus.ACTIVE, error=None, item_count=len(seen))
    if more:
        enqueue_sync(
            state, user_id=user_id, connector_id=connector_id, clock=clock, correlation_id=correlation_id
        )
    return events


def _upsert_file(
    state: Any,
    connector: Connector,
    src: SourceArtifact | None,
    path: Path,
    st: os.stat_result,
    stamp: str,
    now: datetime,
    max_bytes: int,
    full: bool,
    correlation_id: str | None,
) -> bool:
    """Create or update one file source. Returns True when an event was raised."""
    user_id = connector.user_id
    too_big = st.st_size > max_bytes
    sha = "" if too_big else _sha256(path)
    version = f"{stamp}:{sha or 'skipped'}"
    modified = datetime.fromtimestamp(st.st_mtime_ns / 1e9, tz=UTC)
    origin = SourceOrigin(
        connector_id=connector.id,
        external_id=str(path),
        external_version=version[:200],
        uri=str(path),
        title=path.name[:500],
        mime_type=mimetypes.guess_type(path.name)[0],
        modified_at=modified,
        size_bytes=st.st_size,
    )
    content_same = (
        src is not None and src.origin is not None and sha and src.origin.external_version.endswith(":" + sha)
    )
    skipped = {
        "lifecycle": SourceLifecycle.FAILED,
        "failure": FailureCategory.LIMIT_EXCEEDED,
        "error_message": f"skipped: {st.st_size} bytes exceeds the {max_bytes}-byte corpus limit",
    }
    with state.transaction(user_id) as tx:
        if src is None:
            stored = tx.put(
                "source",
                SourceArtifact(
                    id=new_id("src"),
                    user_id=user_id,
                    created_at=now,
                    updated_at=now,
                    kind=SourceKind.FILE,
                    capture_timezone="UTC",
                    captured_at=modified,
                    origin=origin,
                    **(skipped if too_big else {"lifecycle": SourceLifecycle.PROCESSING}),
                ),
            )
        elif content_same and not full:
            # Touched but identical: record the new stamp, keep the version.
            tx.put("source", src.model_copy(update={"origin": origin}), expected_revision=src.revision)
            return False
        else:
            update: dict[str, Any] = {
                "origin": origin,
                "lifecycle": SourceLifecycle.PROCESSING,
                "failure": None,
                "error_message": None,
                "captured_at": modified,
            }
            if not content_same:
                update.update(current_version=src.current_version + 1, chunk_count=None)
            if too_big:
                update.update(skipped)
            stored = tx.put("source", src.model_copy(update=update), expected_revision=src.revision)
        if too_big:
            return False  # skipped with a visible reason; not an ingest, not a dead letter
        tx.append_outbox(
            _event(
                user_id,
                now,
                EventType.SOURCE_INGEST_REQUESTED,
                f"ingest:{stored.id}:{stored.current_version}:{stored.revision}",
                {
                    "source_id": stored.id,
                    "source_version": stored.current_version,
                    "connector_id": connector.id,
                },
                target_id=stored.id,
                correlation_id=correlation_id,
            )
        )
    return True


def _finish(
    state: Any,
    connector: Connector,
    now: datetime,
    *,
    status: ConnectorStatus,
    error: str | None,
    item_count: int | None = None,
) -> None:
    """Record the pass outcome. Re-read first: a PATCH may have landed meanwhile."""
    current = state.get(connector.user_id, "connector", connector.id) or connector
    if current.status is ConnectorStatus.PAUSED:
        status = ConnectorStatus.PAUSED  # the user's pause wins over a pass that was already running
    update: dict[str, Any] = {
        "status": status,
        "last_error": error,
        "last_sync_at": now,
        "next_sync_at": now + timedelta(seconds=current.config.sync_interval_seconds),
        "cursor": now.isoformat(),
    }
    if item_count is not None:
        update["item_count"] = item_count
    with state.transaction(connector.user_id) as tx:
        tx.put("connector", current.model_copy(update=update), expected_revision=current.revision)


__all__ = [
    "DEFAULT_EXCLUDES",
    "MAX_EVENTS_PER_PASS",
    "SECRET_EXCLUDES",
    "enqueue_due_syncs",
    "enqueue_sync",
    "remove_connector",
    "sync_connector",
    "walk",
]
