"""Corpus: parsers, chunking, connector sync, ingestion, hybrid search + citations."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from metis_contracts import (
    AdapterMode,
    Connector,
    ConnectorConfig,
    ConnectorKind,
    ConnectorStatus,
    ErrorCode,
    FixedClock,
    MetisError,
    Task,
    reset_settings_cache,
)
from metis_contracts.enums import FailureCategory, SourceLifecycle
from metis_pipeline.dispatcher import DispatchContext, OutboxDispatcher
from metis_retrieval import (
    EvidenceBudget,
    FixtureEmbedder,
    HybridRetriever,
    LocalEmbedder,
    RetrievalService,
    SqliteMemoryStore,
    build_embedder,
    chunk_text,
    parse_file,
)
from metis_retrieval.connectors import enqueue_sync, remove_connector, sync_connector
from metis_retrieval.handlers import registry
from metis_state.sqlite_store import SqliteStateStore

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
USER, OTHER = "user_a", "user_b"


def text_pdf(path: Path, text: str) -> Path:
    """Minimal one-page PDF with a real text layer (offsets computed)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path


class RealishEmbedder(FixtureEmbedder):
    """Fixture vectors, but declared semantic so the semantic lane runs."""

    mode = AdapterMode.LOCAL
    semantic = True
    model_id = "test-semantic"


@pytest.fixture()
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture()
def state(tmp_path_factory, clock) -> SqliteStateStore:
    reset_settings_cache()
    yield SqliteStateStore(str(tmp_path_factory.mktemp("state") / "s.sqlite"), clock=clock)
    reset_settings_cache()


@pytest.fixture()
def memory() -> SqliteMemoryStore:
    return SqliteMemoryStore(":memory:")


def add_connector(state, root: Path, user=USER, **cfg) -> Connector:
    now = T0
    connector = Connector(
        id="con_docs",
        user_id=user,
        created_at=now,
        updated_at=now,
        kind=ConnectorKind.LOCAL_FOLDER,
        title="Docs",
        config=ConnectorConfig(paths=[str(root)], **cfg),
    )
    with state.transaction(user) as tx:
        return tx.put("connector", connector)


def outbox(state) -> list[tuple[str, str]]:
    rows = state._conn.execute("SELECT event_type, state FROM outbox ORDER BY created_at, id").fetchall()
    return [(r["event_type"], r["state"]) for r in rows]


def drain(state, memory, clock, embedder=None) -> None:
    ctx = DispatchContext(
        state_store=state,
        source_store=None,  # type: ignore[arg-type]
        clock=clock,
        memory_store=memory,
        embedder=embedder or FixtureEmbedder(),
    )
    dispatcher = OutboxDispatcher(registry, ctx)
    while dispatcher.run_once(limit=100).processed:
        pass


def sources(state, user=USER):
    return {s.origin.title: s for s in state.list_sources(user, limit=500).items if s.origin}


# --- parsers + chunking ---------------------------------------------------- #


def test_text_parsers_decode_and_reject_binary(tmp_path):
    (tmp_path / "a.md").write_text("# héllo", encoding="utf-8")
    (tmp_path / "b.csv").write_bytes("caf\xe9,1".encode("latin-1"))
    (tmp_path / "c.bin").write_bytes(b"\x00\x01binary")
    assert parse_file(tmp_path / "a.md") == "# héllo"
    assert parse_file(tmp_path / "b.csv") == "café,1"
    with pytest.raises(MetisError) as err:
        parse_file(tmp_path / "c.bin")
    assert err.value.code == ErrorCode.UNSUPPORTED_MEDIA


def test_pdf_text_layer_corrupt_and_scanned(tmp_path):
    from pypdf import PdfWriter

    assert "quarterly revenue" in parse_file(text_pdf(tmp_path / "ok.pdf", "quarterly revenue"))
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-1.4 garbage, not a pdf")
    with pytest.raises(MetisError) as err:
        parse_file(tmp_path / "bad.pdf")
    assert err.value.code == ErrorCode.VALIDATION_FAILED
    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    writer.write(tmp_path / "scan.pdf")
    with pytest.raises(MetisError) as err:
        parse_file(tmp_path / "scan.pdf")
    assert err.value.code == ErrorCode.UNSUPPORTED_MEDIA


def test_chunks_cite_exact_spans_with_overlap_and_context():
    text = "# Notes\n\n" + " ".join(f"word{i}" for i in range(1500))
    chunks = chunk_text(text, title="notes.md", path="/docs/notes.md")
    assert len(chunks) > 2
    for c in chunks:
        assert text[c.start_char : c.end_char] == c.text
        assert len(c.text) <= 2000
        assert c.span_ref == f"chunk:{c.start_char}-{c.end_char}"
        assert c.embed_text.startswith("notes.md \u203a /docs/notes.md\n")
    assert any(b.start_char < a.end_char for a, b in pairwise(chunks)), "overlap"


# --- fusion ---------------------------------------------------------------- #


class _Fixed(BaseRetriever):
    ids: list[str]
    matcher: str

    def _get_relevant_documents(self, query, *, run_manager):
        return [
            Document(page_content="", metadata={"key": i, "id": i, "matcher": self.matcher}) for i in self.ids
        ]


def test_rrf_fuses_and_records_which_matchers_hit():
    hybrid = HybridRetriever(
        retrievers=[_Fixed(ids=["a", "b"], matcher="semantic"), _Fixed(ids=["c", "b"], matcher="keyword")]
    )
    docs = hybrid.invoke("q")
    assert docs[0].metadata["id"] == "b", "a hit in both lanes outranks a single first place"
    assert docs[0].metadata["matched"] == ["semantic", "keyword"]
    assert {d.metadata["id"]: d.metadata["matched"] for d in docs[1:]} == {
        "a": ["semantic"],
        "c": ["keyword"],
    }
    assert docs[0].metadata["score"] == pytest.approx(0.5 / 62 + 0.5 / 62)


# --- connector ------------------------------------------------------------- #


def test_connector_add_change_delete_and_secrets_excluded(tmp_path, state, clock):
    root = tmp_path / "docs"
    (root / ".ssh").mkdir(parents=True)
    (root / ".ssh" / "config").write_text("Host x")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("x")
    for name in (".env", ".env.local", "id_rsa", "server.pem", "tls.key"):
        (root / name).write_text("secret")
    (root / "notes.md").write_text("ship the corpus")
    (root / "sub").mkdir()
    (root / "sub" / "plan.txt").write_text("plan")
    add_connector(state, root, exclude_globs=["*.txt"], include_globs=[])
    # A user include cannot pull a secret back in.
    with state.transaction(USER) as tx:
        c = state.get(USER, "connector", "con_docs")
        tx.put(
            "connector",
            c.model_copy(update={"config": c.config.model_copy(update={"include_globs": ["*"]})}),
            expected_revision=c.revision,
        )

    assert sync_connector(state, user_id=USER, connector_id="con_docs", clock=clock) == 1
    found = sources(state)
    assert set(found) == {"notes.md"}, "secrets, noise and user-excluded files are never ingested"
    notes = found["notes.md"]
    assert notes.kind.value == "file" and notes.current_version == 1 and notes.capture_timezone == "UTC"
    connector = state.get(USER, "connector", "con_docs")
    assert connector.item_count == 1 and connector.last_sync_at == T0 and connector.next_sync_at > T0

    # Unchanged: nothing raised. Touched but identical: no version bump.
    assert sync_connector(state, user_id=USER, connector_id="con_docs", clock=clock) == 0
    os.utime(root / "notes.md", ns=(1, 1_000_000_000))
    assert sync_connector(state, user_id=USER, connector_id="con_docs", clock=clock) == 0
    assert sources(state)["notes.md"].current_version == 1

    (root / "notes.md").write_text("ship the corpus, then the planner")
    assert sync_connector(state, user_id=USER, connector_id="con_docs", clock=clock) == 1
    assert sources(state)["notes.md"].current_version == 2

    (root / "notes.md").unlink()
    assert sync_connector(state, user_id=USER, connector_id="con_docs", clock=clock) == 1
    assert sources(state) == {}
    types = [t for t, _ in outbox(state)]
    assert types.count("source.ingest_requested") == 2 and types.count("corpus.purge_requested") == 1


def test_bad_root_sets_error_honestly(tmp_path, state, clock):
    add_connector(state, tmp_path / "missing")
    assert sync_connector(state, user_id=USER, connector_id="con_docs", clock=clock) == 0
    connector = state.get(USER, "connector", "con_docs")
    assert connector.status is ConnectorStatus.ERROR and "missing" in connector.last_error
    (tmp_path / "missing").mkdir()
    clock.advance(seconds=1)
    sync_connector(state, user_id=USER, connector_id="con_docs", clock=clock)
    healed = state.get(USER, "connector", "con_docs")
    assert healed.status is ConnectorStatus.ACTIVE and healed.last_error is None


def test_oversized_files_are_skipped_with_a_reason(tmp_path, state, clock, monkeypatch):
    monkeypatch.setenv("METIS_CORPUS_MAX_FILE_BYTES", "10")
    reset_settings_cache()
    (tmp_path / "big.txt").write_text("x" * 50)
    add_connector(state, tmp_path)
    assert sync_connector(state, user_id=USER, connector_id="con_docs", clock=clock) == 0
    big = sources(state)["big.txt"]
    assert big.lifecycle is SourceLifecycle.FAILED and big.failure is FailureCategory.LIMIT_EXCEEDED
    assert "skipped" in big.error_message


# --- ingest + search end to end ------------------------------------------- #


def test_ingest_end_to_end_filename_hit_and_citation(tmp_path, state, memory, clock):
    text_pdf(tmp_path / "Q3-report.pdf", "Revenue grew eleven percent")
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 truncated")
    add_connector(state, tmp_path)
    enqueue_sync(state, user_id=USER, connector_id="con_docs", clock=clock)
    drain(state, memory, clock)

    found = sources(state)
    assert (
        found["Q3-report.pdf"].lifecycle is SourceLifecycle.READY and found["Q3-report.pdf"].chunk_count == 1
    )
    assert found["broken.pdf"].lifecycle is SourceLifecycle.FAILED
    assert found["broken.pdf"].failure is FailureCategory.VALIDATION
    dead = state.list_dead_letters()
    assert len(dead) == 1 and dead[0].attempts == 1, "malformed input dead-letters without retry"

    service = RetrievalService(
        state_store=state, memory_store=memory, embedder=FixtureEmbedder(), clock=clock
    )
    results, degraded = service.search(USER, query="report")
    assert degraded and "keyword search only" in degraded, "fixture embeddings are not semantic"
    assert len(results) == 1, "the file name is keyword-searchable though the body never says 'report'"
    hit = results[0]
    ev = hit.evidence
    assert hit.chunk_id and hit.memory_id is None and hit.kind is None
    assert ev.origin == "file" and ev.title == "Q3-report.pdf" and ev.uri.endswith("Q3-report.pdf")
    assert ev.matched == ["keyword"] and ev.chunk_id == hit.chunk_id
    assert ev.span_ref == f"chunk:{ev.start_char}-{ev.end_char}" and "Revenue" in ev.excerpt
    assert ev.source_id == found["Q3-report.pdf"].id and ev.source_version == 1

    assert service.search(OTHER, query="report")[0] == [], "never another owner's corpus"


def test_semantic_lane_and_stale_versions(tmp_path, state, memory, clock):
    (tmp_path / "a.md").write_text("caching invalidation strategy for the build")
    add_connector(state, tmp_path)
    embedder = RealishEmbedder()
    enqueue_sync(state, user_id=USER, connector_id="con_docs", clock=clock)
    drain(state, memory, clock, embedder)

    service = RetrievalService(state_store=state, memory_store=memory, embedder=embedder, clock=clock)
    results, degraded = service.search(USER, query="caching strategy")
    assert degraded is None
    assert results and results[0].evidence.matched == ["semantic", "keyword"]

    # A newer version without re-indexing: the old chunks are no longer evidence.
    src = sources(state)["a.md"]
    with state.transaction(USER) as tx:
        tx.put("source", src.model_copy(update={"current_version": 2}), expected_revision=src.revision)
    assert service.search(USER, query="caching strategy")[0] == []


def test_planner_evidence_includes_corpus_and_purge_removes_it(tmp_path, state, memory, clock):
    (tmp_path / "runbook.md").write_text("Rotate the deploy keys before the Friday release")
    connector = add_connector(state, tmp_path)
    enqueue_sync(state, user_id=USER, connector_id="con_docs", clock=clock)
    drain(state, memory, clock)
    service = RetrievalService(
        state_store=state, memory_store=memory, embedder=FixtureEmbedder(), clock=clock
    )
    task = Task(id="task_1", user_id=USER, created_at=T0, updated_at=T0, title="Rotate deploy keys")
    evidence = service.evidence_for_task(USER, task, budget=EvidenceBudget(max_tokens=10_000))
    assert evidence and evidence[0].origin == "file" and evidence[0].chunk_id

    assert remove_connector(state, user_id=USER, connector=connector, clock=clock) == 1
    assert state.get(USER, "connector", "con_docs") is None
    drain(state, memory, clock)
    assert memory.corpus.keyword(USER, "deploy", limit=5) == []


def test_corpus_share_caps_imported_hits(tmp_path, state, memory, clock, monkeypatch):
    monkeypatch.setenv("METIS_RETRIEVAL_CORPUS_SHARE", "20")
    reset_settings_cache()
    for i in range(6):
        (tmp_path / f"note{i}.md").write_text(f"deploy checklist item {i}")
    add_connector(state, tmp_path)
    enqueue_sync(state, user_id=USER, connector_id="con_docs", clock=clock)
    drain(state, memory, clock)
    service = RetrievalService(
        state_store=state, memory_store=memory, embedder=FixtureEmbedder(), clock=clock
    )
    assert len(service.search(USER, query="deploy", limit=10)[0]) == 2
    assert len(service.search(USER, query="deploy", limit=10, origins=["file"])[0]) == 6


# --- embedder selection ---------------------------------------------------- #


def test_embed_engine_selection(monkeypatch):
    monkeypatch.delenv("METIS_INFERENCE_ENGINE", raising=False)
    monkeypatch.setenv("METIS_EMBED_ENGINE", "auto")
    monkeypatch.setenv("METIS_LOCAL_EMBED_MODEL", "nomic-embed-text")
    assert isinstance(build_embedder(), LocalEmbedder)
    monkeypatch.setenv("METIS_EMBED_ENGINE", "fixture")
    assert isinstance(build_embedder(), FixtureEmbedder)
    monkeypatch.setenv("METIS_EMBED_ENGINE", "bogus")
    with pytest.raises(ValueError):
        build_embedder()


def test_a_large_pass_is_capped_and_continued(tmp_path, state, clock, monkeypatch):
    import metis_retrieval.connectors as connectors

    monkeypatch.setattr(connectors, "MAX_EVENTS_PER_PASS", 2)
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(f"file {i}")
    add_connector(state, tmp_path)
    assert sync_connector(state, user_id=USER, connector_id="con_docs", clock=clock) == 2
    assert [t for t, _ in outbox(state)].count("connector.sync_requested") == 1, "continuation enqueued"
    clock.advance(seconds=1)
    assert sync_connector(state, user_id=USER, connector_id="con_docs", clock=clock) == 2
    clock.advance(seconds=1)
    assert sync_connector(state, user_id=USER, connector_id="con_docs", clock=clock) == 1
    assert len(sources(state)) == 5
    assert state.get(USER, "connector", "con_docs").item_count == 5
