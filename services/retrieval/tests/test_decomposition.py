"""Query decomposition: routing, parallel fusion, sequential chaining, fallback."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from metis_contracts import Evidence
from metis_contracts.api import SearchResult
from metis_retrieval.decomposition import Decomposer, fuse, needs_decomposition

COMPARE = "compare revenue in the Q3 report and the Q4 report"
CHAIN = "who owns the project mentioned in my launch email"


def hit(name: str, content: str = "text") -> SearchResult:
    return SearchResult(
        chunk_id=f"chk_{name}",
        content=content,
        score=0.5,
        evidence=Evidence(
            source_id=f"src_{name}",
            source_version=1,
            span_ref="chunk:0-4",
            excerpt=content,
            recorded_at=datetime(2026, 11, 22, tzinfo=UTC),
            origin="file",
            chunk_id=f"chk_{name}",
            matched=["keyword"],
        ),
    )


def llm(*replies: object) -> FakeListChatModel:
    return FakeListChatModel(responses=[r if isinstance(r, str) else json.dumps(r) for r in replies])


def test_simple_queries_never_call_the_llm():
    assert not needs_decomposition("Q3 report")
    decomposer = Decomposer(llm("this reply would fail to parse"))
    found = decomposer.search("Q3 report", lambda q: ([hit("a")], None), limit=5)
    assert found.mode == "none" and found.note is None and len(found.results) == 1


def test_parallel_runs_concurrently_and_fuses_citations():
    plan = {"mode": "parallel", "steps": ["Q3 revenue", "Q4 revenue"]}
    barrier = threading.Barrier(3, timeout=5)  # fails unless all three searches overlap
    data = {COMPARE: [hit("both")], "Q3 revenue": [hit("q3"), hit("both")], "Q4 revenue": [hit("q4")]}

    def search(query):
        barrier.wait()
        return data[query], None

    found = Decomposer(llm(plan)).search(COMPARE, search, limit=10)
    assert found.mode == "parallel" and found.sub_queries == ["Q3 revenue", "Q4 revenue"]
    ids = [r.chunk_id for r in found.results]
    assert ids[0] == "chk_both" and sorted(ids) == ["chk_both", "chk_q3", "chk_q4"]


def test_sequential_feeds_each_answer_to_the_next_query():
    plan = {"mode": "sequential", "steps": ["launch email project", "owner of {previous}"]}
    seen = []

    def search(query):
        seen.append(query)
        return [hit(str(len(seen)), "The launch is Project Atlas")], None

    found = Decomposer(llm(plan, {"answer": "Project Atlas"})).search(CHAIN, search, limit=10)
    assert seen == ["launch email project", "owner of Project Atlas"]
    assert found.mode == "sequential" and found.sub_queries == seen and len(found.results) == 2


def test_sequential_stops_honestly_instead_of_inventing_an_answer():
    plan = {"mode": "sequential", "steps": ["launch email project", "owner of {previous}"]}
    seen = []

    def search(query):
        seen.append(query)
        return [hit("a")], None

    found = Decomposer(llm(plan, {"answer": None})).search(CHAIN, search, limit=10)
    assert seen == ["launch email project", CHAIN], "falls back to the whole question"
    assert "stopped at step 2" in found.note


def test_bad_planner_output_falls_back_to_plain_search():
    found = Decomposer(llm("not json")).search(COMPARE, lambda q: ([hit("a")], None), limit=5)
    assert found.mode == "none" and len(found.results) == 1
    assert found.note and found.note.startswith("query decomposition unavailable")


def test_fuse_keeps_one_row_per_citation():
    fused = fuse([[hit("a"), hit("b")], [hit("b")]], limit=5)
    assert [r.chunk_id for r in fused] == ["chk_b", "chk_a"]
