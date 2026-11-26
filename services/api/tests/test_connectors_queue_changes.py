"""Connectors, queue/dead letters, change feed long-poll and hybrid search."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime

from metis_contracts import EventType, OutboxEvent, Task

T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
USER = "demo_authenticated_subject"

OTHER = "someone_else"


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


def create(client, folder, **extra):
    return client.post(
        "/v1/connectors",
        json={"title": "Notes", "config": {"paths": [str(folder)], **extra}},
        headers=key(),
    )


def outbox(adapters, user=USER) -> list[OutboxEvent]:
    store = adapters.state_store
    with store._lock:
        rows = store._conn.execute("SELECT * FROM outbox WHERE user_id=?", (user,)).fetchall()
    return [store._outbox_from_row(r) for r in rows]


def test_create_rejects_bad_paths(client, tmp_path):
    assert create(client, "relative/dir").status_code == 422
    assert create(client, tmp_path / "missing").status_code == 422
    (tmp_path / "file.txt").write_text("x")
    assert create(client, tmp_path / "file.txt").status_code == 422
    secrets = tmp_path / ".ssh"
    secrets.mkdir()
    refused = create(client, secrets)
    assert refused.status_code == 422
    assert ".ssh" in refused.json()["message"]
    no_key = client.post("/v1/connectors", json={"title": "x", "config": {"paths": [str(tmp_path)]}})
    assert no_key.status_code == 422


def test_create_writes_connector_and_full_sync(client, adapters, tmp_path):
    response = create(client, tmp_path)
    assert response.status_code == 201
    connector = response.json()
    assert connector["user_id"] == USER
    assert connector["kind"] == "local_folder"
    assert connector["config"]["paths"] == [str(tmp_path.resolve())]
    events = [e for e in outbox(adapters) if e.event_type == EventType.CONNECTOR_SYNC_REQUESTED]
    assert len(events) == 1
    assert events[0].payload == {"connector_id": connector["id"], "full": True}
    page = client.get("/v1/connectors").json()
    assert [c["id"] for c in page["items"]] == [connector["id"]]


def test_patch_pause_and_stale_revision(client, tmp_path):
    connector = create(client, tmp_path).json()
    paused = client.patch(
        f"/v1/connectors/{connector['id']}", json={"expected_revision": 1, "status": "paused"}
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    stale = client.patch(
        f"/v1/connectors/{connector['id']}", json={"expected_revision": 1, "status": "active"}
    )
    assert stale.status_code == 409
    # A paused connector does not accept a sync it would skip.
    assert client.post(f"/v1/connectors/{connector['id']}/sync", headers=key()).status_code == 422


def test_sync_now_and_delete(client, adapters, tmp_path):
    connector = create(client, tmp_path).json()
    accepted = client.post(f"/v1/connectors/{connector['id']}/sync", headers=key())
    assert accepted.status_code == 202
    assert accepted.json()["job_id"] in {e.id for e in outbox(adapters)}
    assert client.delete(f"/v1/connectors/{connector['id']}").status_code == 204
    assert client.get(f"/v1/connectors/{connector['id']}").status_code == 404
    assert client.delete(f"/v1/connectors/{connector['id']}").status_code == 404


def _dead(adapters, event_id: str, user: str) -> None:
    store = adapters.state_store
    event = OutboxEvent(
        id=event_id,
        user_id=user,
        created_at=T0,
        updated_at=T0,
        event_type=EventType.SOURCE_INGEST_REQUESTED,
        correlation_id="corr",
        idempotency_key=event_id,
        available_at=T0,
    )
    with store.transaction(user) as tx:
        tx.append_outbox(event)
    store.complete_outbox(
        event_id, user_id=user, state="dead_letter", failure="validation", error="corrupt pdf"
    )


def test_dead_letters_are_owner_scoped(client, adapters):
    _dead(adapters, "obx_mine", USER)
    _dead(adapters, "obx_theirs", OTHER)
    items = client.get("/v1/queue/dead-letters").json()["items"]
    assert [(i["event_id"], i["lane"], i["error_message"]) for i in items] == [
        ("obx_mine", "ingest", "corrupt pdf")
    ]
    ingest = next(lane for lane in client.get("/v1/queue").json()["lanes"] if lane["lane"] == "ingest")
    assert ingest["dead_letter"] == 1
    url = "/v1/queue/dead-letters/{}/retry"
    assert client.post(url.format("obx_mine")).status_code == 422, "retry requires an Idempotency-Key"
    assert client.post(url.format("obx_theirs"), headers=key()).status_code == 404
    same = key()
    assert client.post(url.format("obx_mine"), headers=same).status_code == 202
    replay = client.post(url.format("obx_mine"), headers=same)
    assert replay.status_code == 202 and replay.json() == {"event_id": "obx_mine"}, "double-click replays"
    assert client.post(url.format("obx_mine"), headers=key()).status_code == 404, "no longer dead"
    assert client.get("/v1/queue/dead-letters").json()["items"] == []


def _write_task(adapters) -> None:
    task = Task(id="task_feed", user_id=USER, created_at=T0, updated_at=T0, title="feed me")
    with adapters.state_store.transaction(USER) as tx:
        tx.put("task", task)


def test_changes_cursor_bootstrap_and_reset(client, adapters):
    _write_task(adapters)
    head = client.get("/v1/changes").json()
    assert head["changes"] == [] and head["cursor"] >= 1 and head["queue"]["lanes"]
    history = client.get("/v1/changes?after=0&wait=0").json()
    assert any(c["kind"] == "task" and c["id"] == "task_feed" for c in history["changes"])
    assert history["cursor"] == head["cursor"]
    reset = client.get(f"/v1/changes?after={head['cursor'] + 100}").json()
    assert reset["reset"] is True and reset["cursor"] == head["cursor"]


def test_changes_long_poll_times_out(client):
    started = time.monotonic()
    assert client.get("/v1/changes?after=0&wait=0").json()["changes"] == []
    assert time.monotonic() - started < 0.5
    started = time.monotonic()
    body = client.get("/v1/changes?after=0&wait=1").json()
    assert body["changes"] == [] and body["queue"] is not None
    assert 0.9 <= time.monotonic() - started < 3


def test_changes_long_poll_returns_on_change_without_blocking(client, adapters):
    cursor = client.get("/v1/changes").json()["cursor"]
    result: dict = {}

    def poll() -> None:
        started = time.monotonic()
        result["body"] = client.get(f"/v1/changes?after={cursor}&wait=10").json()
        result["elapsed"] = time.monotonic() - started

    poller = threading.Thread(target=poll)
    poller.start()
    time.sleep(0.3)
    started = time.monotonic()
    assert client.get("/v1/queue").status_code == 200
    assert time.monotonic() - started < 0.5, "a waiting long-poll must not block other requests"
    _write_task(adapters)
    poller.join(timeout=5)
    assert [c["id"] for c in result["body"]["changes"]] == ["task_feed"]
    assert result["elapsed"] < 3


def test_search_returns_file_citations(client, worker, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    report = folder / "quarterly_report.md"
    report.write_text("# Planning\n\nThe zeppelin budget review is due next week.\n")
    create(client, folder)
    worker(cycles=10)
    body = client.post("/v1/search", json={"query": "zeppelin"}).json()
    assert body["mode_used"] == "lexical", "the fixture embedder is not semantic"
    assert body["degraded_reason"].startswith("keyword search only")
    assert body["index_available"] is True
    hit = next(r for r in body["results"] if r["chunk_id"])
    evidence = hit["evidence"]
    assert evidence["uri"] == str(report.resolve())
    assert "keyword" in evidence["matched"]
    assert evidence["source_id"] and evidence["span_ref"]
    # A memory-kind filter keeps memories only.
    filtered = client.post("/v1/search", json={"query": "zeppelin", "kinds": ["decision"]}).json()
    assert all(r["chunk_id"] is None for r in filtered["results"])


def test_health_reports_search_mode(client):
    adapters = client.get("/v1/health").json()["adapters"]
    assert adapters["semantic_search"] == "keyword_only"
    assert adapters["embedder_engine"] == "FixtureEmbedder"
