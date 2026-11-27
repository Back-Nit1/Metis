"""Shared server: folders arrive as desktop uploads; the server never reads a client path."""

from pathlib import Path

import pytest
from metis_contracts import ErrorCode, MetisError, Mode, settings
from metis_retrieval import connectors, handlers

ROOT = "/Users/alice/Notes"


class _Server:
    """Real settings, server mode."""

    mode = Mode.SERVER

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def server(monkeypatch):
    cfg = _Server(settings())
    for target in (
        "metis_api.routes.connectors.settings",
        "metis_retrieval.connectors.settings",
        "metis_retrieval.handlers.settings",
    ):
        monkeypatch.setattr(target, lambda: cfg)
    return cfg


def _connector(client, paths=(ROOT,)):
    result = client.post(
        "/v1/connectors",
        headers={"Idempotency-Key": f"conn-{abs(hash(paths))}"},
        json={"kind": "local_folder", "title": "notes", "config": {"paths": list(paths)}},
    )
    assert result.status_code == 201, result.text
    return result.json()["id"]


def test_server_registers_a_device_folder_without_reading_or_syncing_it(client, server, adapters):
    conn = _connector(client, ("/var/lib/metis",))
    # A name only: no server-side sync is queued and "sync now" is refused.
    assert adapters.state_store.queue_stats(None).lanes[1].pending == 0
    sync = client.post(f"/v1/connectors/{conn}/sync", headers={"Idempotency-Key": "s1"})
    assert sync.status_code == 422, sync.text
    bad = client.post(
        "/v1/connectors",
        headers={"Idempotency-Key": "ssh"},
        json={"kind": "local_folder", "title": "keys", "config": {"paths": ["/Users/alice/.ssh"]}},
    )
    assert bad.status_code == 422, bad.text


def test_desktop_upload_is_ingested_and_searchable(client, server, worker, adapters):
    conn = _connector(client)
    listing = {
        "files": [
            {"path": f"{ROOT}/plan.md", "size_bytes": 40, "modified_ns": 1},
            {"path": f"{ROOT}/.env", "size_bytes": 10, "modified_ns": 1},
            {"path": "/etc/passwd", "size_bytes": 10, "modified_ns": 1},
        ]
    }
    manifest = client.post(f"/v1/connectors/{conn}/manifest", json=listing).json()
    assert manifest == {"upload": [f"{ROOT}/plan.md"], "tombstoned": 0, "rejected": 2}

    body = b"Zephyr rollout decision: ship behind a flag."
    put = client.put(
        f"/v1/connectors/{conn}/files",
        params={"path": f"{ROOT}/plan.md", "modified_ns": 1},
        content=body,
    )
    assert put.status_code == 204, put.text
    outside = client.put(
        f"/v1/connectors/{conn}/files", params={"path": "/etc/passwd", "modified_ns": 1}, content=b"x"
    )
    assert outside.status_code == 403, outside.text

    worker()
    found = client.post("/v1/search", json={"query": "Zephyr rollout"}).json()
    assert [r["evidence"]["origin"] for r in found["results"]] == ["file"]

    # Unchanged listing asks for nothing; a file gone from the listing is removed.
    again = {"files": [{"path": f"{ROOT}/plan.md", "size_bytes": len(body), "modified_ns": 1}]}
    assert client.post(f"/v1/connectors/{conn}/manifest", json=again).json()["upload"] == []
    gone = client.post(f"/v1/connectors/{conn}/manifest", json={"files": []}).json()
    assert gone["tombstoned"] == 1
    worker()
    assert client.post("/v1/search", json={"query": "Zephyr rollout"}).json()["results"] == []
    assert not any(Path(adapters.source_store._root).rglob("files/*")), "uploaded bytes purged"


def test_preexisting_desktop_jobs_cannot_read_shared_server(server, adapters):
    from unittest.mock import Mock

    state = Mock()
    assert connectors.enqueue_due_syncs(state, Mock()) == 0
    state.due_connector_ids.assert_not_called()
    with pytest.raises(MetisError):
        connectors.sync_connector(state, user_id="alice", connector_id="old-local", clock=Mock())

    # An old path-based source (no uploaded object) is refused before any disk read.
    source = Mock(object_ref=None, current_version=1)
    context = Mock(memory_store=adapters.memory_store)
    context.state_store.get.return_value = source
    envelope = Mock(
        user_id="alice", payload={"source_id": "src_old", "source_version": 1, "connector_id": "conn_old"}
    )
    with pytest.raises(MetisError) as raised:
        handlers.handle_source_ingest(envelope, context)
    assert raised.value.code is ErrorCode.FORBIDDEN
