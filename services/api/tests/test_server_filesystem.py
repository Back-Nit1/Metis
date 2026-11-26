from unittest.mock import Mock

import pytest
from metis_contracts import MetisError, Mode
from metis_retrieval import connectors, handlers


def test_remote_user_cannot_register_server_directory(client, monkeypatch):
    # Override only the routing guard's deployment mode. Authentication and
    # request validation still run through the normal API test app.
    cfg = Mock(mode=Mode.SERVER)
    monkeypatch.setattr("metis_api.routes.connectors.settings", lambda: cfg)
    result = client.post(
        "/v1/connectors",
        headers={"Idempotency-Key": "read-server"},
        json={"kind": "local_folder", "title": "server disk", "config": {"paths": ["/var/lib/metis"]}},
    )
    assert result.status_code == 403, result.text


def test_preexisting_desktop_jobs_cannot_read_shared_server(monkeypatch):
    cfg = Mock(mode=Mode.SERVER)
    monkeypatch.setattr(connectors, "settings", lambda: cfg)
    monkeypatch.setattr(handlers, "settings", lambda: cfg)
    state = Mock()
    assert connectors.enqueue_due_syncs(state, Mock()) == 0
    state.due_connector_ids.assert_not_called()
    with pytest.raises(MetisError):
        connectors.sync_connector(state, user_id="alice", connector_id="old-local", clock=Mock())
    with pytest.raises(MetisError):
        handlers.handle_source_ingest(Mock(), Mock())
    state.get.assert_not_called()
