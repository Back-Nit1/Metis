"""Real path: API -> outbox -> dispatcher -> parser -> dead letter -> retry."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from metis_contracts.enums import SourceLifecycle
from metis_pipeline.dispatcher import DispatchContext, OutboxDispatcher
from metis_pipeline.local_worker import build_registry
from metis_retrieval import handlers

USER = "demo_authenticated_subject"
MALFORMED = b"%PDF-1.4 truncated"


def valid_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


@pytest.fixture()
def docs(tmp_path) -> Path:
    """Separate from tmp_path, which also holds the test databases."""
    folder = tmp_path / "docs"
    folder.mkdir()
    return folder


@pytest.fixture()
def drain(adapters, clock):
    context = DispatchContext(
        state_store=adapters.state_store,
        source_store=adapters.source_store,
        clock=clock,
        memory_store=adapters.memory_store,
        embedder=adapters.embedder,
    )
    dispatcher = OutboxDispatcher(build_registry(), context)

    def run() -> None:
        for _ in range(20):  # step past retry backoff; bounded so a bug cannot hang
            dispatcher.run_once(limit=100)
            clock.advance(seconds=1000)

    return run


@pytest.fixture()
def calls(monkeypatch):
    """Counts real parses, which is the work idempotency must not repeat."""
    seen = {"parse": 0}
    real = handlers.parse_file

    def counted(path: Path) -> str:
        seen["parse"] += 1
        return real(path)

    monkeypatch.setattr(handlers, "parse_file", counted)
    return seen


def connect(client, folder: Path) -> None:
    body = {"title": "Docs", "config": {"paths": [str(folder)]}}
    assert client.post("/v1/connectors", json=body, headers=key()).status_code == 201


def dead(client) -> list[dict]:
    return client.get("/v1/queue/dead-letters").json()["items"]


def source(adapters, title: str):
    items = adapters.state_store.list_sources(USER, limit=100).items
    return next(s for s in items if s.origin and s.origin.title == title)


def retry(client, event_id: str, headers: dict[str, str] | None = None):
    return client.post(f"/v1/queue/dead-letters/{event_id}/retry", headers=headers or key())


def test_malformed_pdf_dead_letters_once_without_auto_retry(client, adapters, drain, calls, docs):
    (docs / "broken.pdf").write_bytes(MALFORMED)
    connect(client, docs)
    drain()

    [letter] = dead(client)
    assert letter["title"] == "broken.pdf"
    assert letter["lane"] == "ingest" and letter["failure"] == "validation"
    assert letter["attempts"] == 1 and calls["parse"] == 1, "permanent failures are not retried"
    assert source(adapters, "broken.pdf").lifecycle is SourceLifecycle.FAILED
    lanes = {lane["lane"]: lane for lane in client.get("/v1/queue").json()["lanes"]}
    assert lanes["ingest"]["dead_letter"] == 1


def test_retry_of_still_malformed_pdf_returns_to_dead_letters(client, drain, calls, docs):
    (docs / "broken.pdf").write_bytes(MALFORMED)
    connect(client, docs)
    drain()
    [letter] = dead(client)

    assert retry(client, letter["event_id"]).status_code == 202
    assert dead(client) == []
    drain()

    [again] = dead(client)
    assert again["event_id"] == letter["event_id"] and again["attempts"] == 1
    assert calls["parse"] == 2, "an explicit retry really reruns the parser"


def test_repaired_pdf_retry_succeeds_and_is_searchable(client, adapters, drain, docs):
    path = docs / "broken.pdf"
    path.write_bytes(MALFORMED)
    connect(client, docs)
    drain()
    [letter] = dead(client)

    path.write_bytes(valid_pdf("Quarterly revenue recovered"))
    assert retry(client, letter["event_id"]).status_code == 202
    drain()

    assert dead(client) == []
    repaired = source(adapters, "broken.pdf")
    assert repaired.lifecycle is SourceLifecycle.READY and repaired.chunk_count == 1
    hits = client.post("/v1/search", json={"query": "revenue"}).json()["results"]
    assert [hit["evidence"]["title"] for hit in hits] == ["broken.pdf"]


def test_double_click_retry_requeues_and_parses_once(client, drain, calls, docs):
    (docs / "broken.pdf").write_bytes(MALFORMED)
    connect(client, docs)
    drain()
    [letter] = dead(client)
    same = key()

    first, second = retry(client, letter["event_id"], same), retry(client, letter["event_id"], same)
    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json() == second.json()
    drain()
    assert calls["parse"] == 2, "initial attempt + one retry"

    # The key is spent: replaying it after the new failure must not requeue.
    assert retry(client, letter["event_id"], same).status_code == 202
    assert len(dead(client)) == 1
    assert retry(client, letter["event_id"]).status_code == 202, "a new key is a new retry"


def test_job_failing_after_work_is_done_does_not_redo_it(client, adapters, drain, calls, docs, monkeypatch):
    (docs / "good.pdf").write_bytes(valid_pdf("Durable result"))
    real_mark, crashed = handlers._mark, []

    def crash_once(context, record, **update):
        if update.get("lifecycle") is SourceLifecycle.READY and not crashed:
            crashed.append(True)
            raise RuntimeError("worker died after indexing")
        return real_mark(context, record, **update)

    monkeypatch.setattr(handlers, "_mark", crash_once)
    embeds = []
    real_embed = adapters.embedder.embed
    monkeypatch.setattr(adapters.embedder, "embed", lambda t: (embeds.append(len(t)), real_embed(t))[1])
    connect(client, docs)
    drain()

    assert crashed and calls["parse"] == 1 and len(embeds) == 1, "the redelivered job reused the receipt"
    finished = source(adapters, "good.pdf")
    assert finished.lifecycle is SourceLifecycle.READY and finished.chunk_count == 1
    assert dead(client) == []


def test_changed_bytes_are_never_skipped(client, adapters, drain, calls, docs):
    path = docs / "note.md"
    path.write_text("first version")
    connect(client, docs)
    drain()
    assert calls["parse"] == 1

    path.write_text("second version, longer")
    connector = client.get("/v1/connectors").json()["items"][0]
    assert client.post(f"/v1/connectors/{connector['id']}/sync", headers=key()).status_code == 202
    drain()

    assert calls["parse"] == 2
    hits = client.post("/v1/search", json={"query": "longer"}).json()["results"]
    assert len(hits) == 1
