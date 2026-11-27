import threading

import pytest
from metis_api.auth import Principal
from metis_contracts import TranscriptSegment, reset_settings_cache
from metis_speech.server import SpeechServer


class Decoder:
    def transcribe(self, audio, **kwargs):
        return (
            "hello",
            [TranscriptSegment(index=0, start_ms=0, end_ms=100, text="hello")],
            {"adapter_mode": "cloud", "model_id": "test"},
        )


@pytest.fixture
def speech(monkeypatch):
    secret = "server-only-secret-" * 3
    server = SpeechServer(Decoder(), secret)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.start_loading()
    assert server.ready_event.wait(5)
    monkeypatch.setenv("METIS_SPEECH_ENDPOINT", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("METIS_SPEECH_SECRET", secret)
    reset_settings_cache()
    yield server
    server.shutdown()
    server.server_close()
    reset_settings_cache()


def test_hosted_dictation_never_exposes_shared_secret_and_isolates_users(client, speech, monkeypatch):
    monkeypatch.setattr(
        "metis_api.browser_session.principal_from_header",
        lambda value: Principal(value.removeprefix("Bearer "), "firebase"),
    )
    alice = {"Authorization": "Bearer alice"}
    # Caller-supplied owner is deliberately ignored by the API proxy.
    bob = {"Authorization": "Bearer bob", "X-Metis-Flow-Owner": "alice"}
    created = client.post("/v1/flow/sessions", headers=alice, json={})
    assert created.status_code == 200, created.text
    assert "secret" not in created.text
    path = "/v1/flow/sessions/" + created.json()["session_id"]
    assert client.post("/v1/flow/sessions", headers=bob, json={}).status_code == 429
    assert client.post(path + "/audio", headers=bob, content=b"\0\0").status_code == 404
    assert client.post(path + "/finish", headers=bob).status_code == 404
    assert client.delete(path, headers=bob).status_code == 404
    assert client.post(path + "/audio", headers=alice, content=b"\1\0" * 1600).status_code == 200
    result = client.post(path + "/finish", headers=alice)
    assert result.status_code == 200, result.text
    assert result.json()["raw_text"] == "hello"
    assert client.post("/v1/flow/sessions", headers=bob, json={}).status_code == 200
