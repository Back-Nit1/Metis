#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/../../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

python3 - "$tmp/queue.sqlite" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as conn:
    conn.execute("CREATE TABLE outbox (state TEXT, available_at TEXT, lease_expires_at TEXT)")
    conn.executemany(
        "INSERT INTO outbox VALUES (?, ?, ?)",
        [
            ("pending", "2026-11-22T19:00:00+00:00", None),
            ("leased", "2026-11-22T19:00:00+00:00", "2026-11-22T19:01:00+00:00"),
            ("dead_letter", "2026-11-22T19:00:00+00:00", None),
        ],
    )
PY

output="$(python3 "$root/ops/gcp/monitoring/queue-observer.py" --database "$tmp/queue.sqlite")"
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["event"] == "metis_queue_stats"; assert d["pending"] == 2; assert d["dead_letter"] == 1; assert d["backlog"] == 3; assert d["oldest_ready_age_seconds"] > 0' "$output"
