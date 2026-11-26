"""Live Ollama checks. Run by hand: see ops/ollama-rag-verification.md."""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import UTC, datetime

import pytest
from metis_contracts import Connector, ConnectorConfig, ConnectorKind, FixedClock, reset_settings_cache
from metis_pipeline.dispatcher import DispatchContext, OutboxDispatcher
from metis_retrieval import RetrievalService, SqliteMemoryStore, build_embedder, semantic_available
from metis_retrieval.connectors import enqueue_sync
from metis_retrieval.handlers import registry
from metis_state.sqlite_store import SqliteStateStore

pytestmark = pytest.mark.integration

ENDPOINT = os.environ.get("METIS_LOCAL_LLM_ENDPOINT", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("METIS_LOCAL_EMBED_MODEL", "nomic-embed-text")
CHAT_MODEL = os.environ.get("METIS_LOCAL_LLM_MODEL", "")
USER = "user_live"
T0 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def installed() -> set[str]:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/api/tags", timeout=2) as response:
            names = [m["name"] for m in json.load(response)["models"]]
    except OSError:
        pytest.skip(f"Ollama is not reachable at {ENDPOINT}")
    return {*names, *(n.split(":")[0] for n in names)}


@pytest.fixture()
def service(tmp_path, monkeypatch):
    if EMBED_MODEL not in installed():
        pytest.skip(f"embedding model {EMBED_MODEL} is not pulled")
    monkeypatch.setenv("METIS_EMBED_ENGINE", "local")
    monkeypatch.setenv("METIS_LOCAL_EMBED_MODEL", EMBED_MODEL)
    monkeypatch.setenv("METIS_LOCAL_LLM_ENDPOINT", ENDPOINT)
    if CHAT_MODEL:
        monkeypatch.setenv("METIS_LOCAL_LLM_MODEL", CHAT_MODEL)
        monkeypatch.setenv("METIS_QUERY_DECOMPOSITION", "local")
    reset_settings_cache()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "garage.md").write_text("The mechanic replaced the brakes on my car last Tuesday.")
    (docs / "kitchen.md").write_text("Sourdough needs a long overnight fermentation in the fridge.")
    (docs / "q3.md").write_text("Third quarter revenue was 11 million dollars.")
    (docs / "q4.md").write_text("Fourth quarter revenue was 14 million dollars.")

    clock = FixedClock(T0)
    state = SqliteStateStore(str(tmp_path / "state.sqlite"), clock=clock)
    memory = SqliteMemoryStore(str(tmp_path / "memory.sqlite"))
    embedder = build_embedder()
    assert semantic_available(embedder), "the live test must use a real embedding model"
    connector = Connector(
        id="con_live",
        user_id=USER,
        created_at=T0,
        updated_at=T0,
        kind=ConnectorKind.LOCAL_FOLDER,
        title="Live",
        config=ConnectorConfig(paths=[str(docs)]),
    )
    with state.transaction(USER) as tx:
        tx.put("connector", connector)
    enqueue_sync(state, user_id=USER, connector_id="con_live", clock=clock)
    context = DispatchContext(
        state_store=state, source_store=None, clock=clock, memory_store=memory, embedder=embedder
    )  # type: ignore[arg-type]
    dispatcher = OutboxDispatcher(registry, context)
    while dispatcher.run_once(limit=50).processed:
        pass
    yield RetrievalService(state_store=state, memory_store=memory, embedder=embedder, clock=clock)
    reset_settings_cache()


def test_semantic_search_finds_a_paraphrase_with_no_shared_keyword(service):
    results, degraded = service.search(USER, query="automobile repair", limit=2)
    assert degraded is None
    assert results[0].evidence.title == "garage.md"
    assert "semantic" in results[0].evidence.matched


def test_parallel_decomposition_on_a_real_chat_model(service):
    if not CHAT_MODEL:
        pytest.skip("set METIS_LOCAL_LLM_MODEL to test decomposition")
    found = service.search_decomposed(USER, query="compare revenue in the third and fourth quarter", limit=4)
    assert found.note is None
    assert found.mode == "parallel" and len(found.sub_queries) >= 2
    assert {"q3.md", "q4.md"} <= {r.evidence.title for r in found.results}
