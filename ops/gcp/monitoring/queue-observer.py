#!/usr/bin/env python3
"""Emit one privacy-safe queue-depth sample for Cloud Logging metrics."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def sample(path: Path, now: datetime) -> dict[str, int | str]:
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        rows = conn.execute(
            """SELECT state, created_at, lease_expires_at
                 FROM outbox
                WHERE state IN ('pending', 'leased', 'failed', 'dead_letter')"""
        ).fetchall()

    counts = {"pending": 0, "leased": 0, "failed": 0, "dead_letter": 0}
    waiting_since: list[datetime] = []
    for state, created_at, lease_expires_at in rows:
        created = parse_instant(created_at)
        lease_expires = parse_instant(lease_expires_at)
        if state == "leased" and (lease_expires is None or lease_expires <= now):
            state = "pending"
        counts[state] += 1
        if state in {"pending", "failed", "dead_letter"}:
            waiting_since.append(created or now)

    oldest_age = max(0, int((now - min(waiting_since)).total_seconds())) if waiting_since else 0
    return {
        "event": "metis_queue_stats",
        **counts,
        "backlog": len(waiting_since),
        "oldest_ready_age_seconds": oldest_age,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("/var/lib/metis/metis.sqlite"))
    args = parser.parse_args()
    print(json.dumps(sample(args.database, datetime.now(UTC)), separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
