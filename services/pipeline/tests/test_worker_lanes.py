"""Lane routing in the dispatcher and the multi-process worker lifecycle."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from metis_contracts import EventType, FixedClock, OutboxEvent, new_id, reset_settings_cache
from metis_pipeline.dispatcher import DispatchContext, HandlerRegistry, OutboxDispatcher, lane_of
from metis_state.sqlite_store import SqliteStateStore

T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
USER = "user_a"


def test_lane_of():
    assert lane_of(EventType.CONNECTOR_SYNC_REQUESTED) == "ingest"
    assert lane_of(EventType.CORPUS_PURGE_REQUESTED) == "ingest"
    assert lane_of(EventType.TASK_CHANGED) == "interactive"


def test_dispatcher_only_runs_its_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("METIS_INGEST_MAX_IN_FLIGHT", "1")
    reset_settings_cache()
    clock = FixedClock(T0)
    store = SqliteStateStore(str(tmp_path / "s.sqlite"), clock=clock)
    for event_type in (
        EventType.SOURCE_INGEST_REQUESTED,
        EventType.SOURCE_INGEST_REQUESTED,
        EventType.TASK_CHANGED,
    ):
        with store.transaction(USER) as tx:
            tx.append_outbox(
                OutboxEvent(
                    id=new_id("obx"),
                    user_id=USER,
                    created_at=T0,
                    updated_at=T0,
                    event_type=event_type,
                    correlation_id="c",
                    idempotency_key=new_id("k"),
                    available_at=T0,
                )
            )
    seen: list[str] = []
    registry = HandlerRegistry()
    for event_type in (EventType.SOURCE_INGEST_REQUESTED, EventType.TASK_CHANGED):
        registry.register(event_type)(lambda env, ctx: seen.append(env.event_type))
    context = DispatchContext(state_store=store, source_store=None, clock=clock, worker_id="w")
    try:
        assert OutboxDispatcher(registry, context, lane="ingest").run_once().processed == 1, "in-flight cap"
        assert OutboxDispatcher(registry, context, lane="interactive").run_once().processed == 1
        assert seen == [EventType.SOURCE_INGEST_REQUESTED, EventType.TASK_CHANGED]
    finally:
        reset_settings_cache()


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM semantics")
def test_multi_process_worker_starts_once_and_stops_on_sigterm(tmp_path):
    env = {
        **os.environ,
        "METIS_MODE": "local",
        "METIS_SQLITE_PATH": str(tmp_path / "m.sqlite"),
        "METIS_LOCAL_OBJECTS_DIR": str(tmp_path / "obj"),
    }
    p = subprocess.Popen(
        [sys.executable, "-m", "metis_pipeline.local_worker", "--processes", "2", "--interval", "0.2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        record = json.loads(p.stdout.readline())
        assert record["event"] == "started" and record["pid"] == p.pid
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=20)
    finally:
        p.kill()
    assert p.returncode == 0, err.decode()[-2000:]
    assert out == b"", "exactly one readiness record"
    log = err.decode()
    assert "worker process 0 (interactive) stopped" in log and "worker process 1 (ingest) stopped" in log
