from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from metis_speech.model_polish import OllamaPolisher, from_env


class _Ollama(BaseHTTPRequestHandler):
    reply = ""
    request: ClassVar[dict] = {}

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        type(self).request = json.loads(self.rfile.read(length))
        body = json.dumps({"message": {"content": json.dumps({"text": type(self).reply})}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def ollama():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_model_polish_is_structured_and_final_only(ollama: ThreadingHTTPServer) -> None:
    _Ollama.reply = "send the launch plan Friday at 3pm"
    polisher = OllamaPolisher("qwen-test", endpoint=f"http://127.0.0.1:{ollama.server_port}")
    result = polisher.polish(
        raw_text="um send the launch plan Friday at 3pm",
        deterministic_text="Send the launch plan Friday at 3pm.",
        app="Mail",
        style="standard",
        protected_terms=(),
    )
    assert result.text == "Send the launch plan Friday at 3pm."
    assert _Ollama.request["stream"] is False
    assert _Ollama.request["format"]["required"] == ["text"]
    assert _Ollama.request["options"]["temperature"] == 0


@pytest.mark.parametrize(
    "reply",
    [
        "Send it Friday at 4pm.",  # invented time
        "I agree.",  # removed negation
        "Do not send it to new@example.com.",  # invented address
        "Ask them about it.",  # removed protected name
        "Do not send it to Cerno Thursday at 3pm.",  # changed named date
        "Do not send it to Cerno Friday.",  # removed time
    ],
)
def test_semantic_drift_is_rejected(ollama: ThreadingHTTPServer, reply: str) -> None:
    _Ollama.reply = reply
    polisher = OllamaPolisher("qwen-test", endpoint=f"http://127.0.0.1:{ollama.server_port}")
    with pytest.raises(ValueError):
        polisher.polish(
            raw_text="do not send it to Cerno Friday at 3pm",
            deterministic_text="Do not send it to Cerno Friday at 3pm.",
            app=None,
            style="standard",
            protected_terms=("Cerno",),
        )


def test_model_cleanup_is_opt_in_and_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("METIS_FLOW_MODEL", raising=False)
    assert from_env() is None
    monkeypatch.setenv("METIS_FLOW_MODEL", "qwen-test")
    monkeypatch.setenv("METIS_FLOW_MODEL_ENDPOINT", "https://models.example")
    with pytest.raises(ValueError, match="loopback"):
        from_env()
