"""Metis authoritative state adapters."""

from __future__ import annotations

from typing import Any

from .object_store import (
    LocalSourceStore,
    build_key,
    build_source_store,
    extension_for,
    owner_prefix,
)
from .sqlite_store import PLANNING_RELEVANT_KINDS, SqliteStateStore

__all__ = [
    "PLANNING_RELEVANT_KINDS",
    "LocalSourceStore",
    "SqliteStateStore",
    "build_key",
    "build_source_store",
    "build_state_store",
    "extension_for",
    "owner_prefix",
]


def build_state_store(clock: Any | None = None) -> Any:
    """Return the StateStore: SQLite in both local and server mode."""
    return SqliteStateStore(clock=clock)
