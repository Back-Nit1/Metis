"""Retrieval evaluation: does the right memory come back, and nothing unsafe?

Loads a labelled corpus into a throwaway SQLite state store + memory index,
runs every query through ``RetrievalService.retrieve_supporting_memories``
(so hydration, staleness and ownership checks are exercised) and scores each
retrieval mode on the same data:

* recall@20 (candidate list) and recall@6 (what can reach the prompt)
* MRR: how high the first correct memory ranks
* forbidden: stale-version, deleted, out-of-scope or other-user memories returned (must be 0)
* stale_first: a superseded decision ranked above the current one
* false_hits: a "nothing stored about this" query that still returned results
* ambiguity_seen: an ambiguous query whose top 6 spans every plausible project
* leaks / bad_links: other-owner memory or unresolvable source link (must be 0)

Tune on ``dev``; report ``heldout``. The fixture embedder is a hashed bag of
words, so its dense numbers only prove the harness runs.

    python -m metis_retrieval.evaluate                      # fixture embedder
    python -m metis_retrieval.evaluate --embedder configured  # METIS_INFERENCE engine
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from metis_contracts import (
    FixedClock,
    Memory,
    MemoryKind,
    MemoryValidity,
    SourceArtifact,
    SourceRef,
)
from metis_contracts.entities import TranscriptRevision
from metis_contracts.enums import SourceKind, SourceLifecycle
from metis_state.sqlite_store import SqliteStateStore

from .embedder import FixtureEmbedder, build_embedder, content_hash
from .memory_store import SqliteMemoryStore
from .service import RetrievalService

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "retrieval_eval.json"
MODES = ("dense", "lexical", "hybrid")
T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
CANDIDATES, EVIDENCE = 20, 6
HARD_METRICS = ("forbidden", "leaks", "bad_links")


class _ModeStore:
    """Forces one retrieval arm on the shared index without copying it."""

    def __init__(self, inner: SqliteMemoryStore, mode: str) -> None:
        self._inner, self._mode = inner, mode
        self.supports_lexical = mode != "dense"

    def health(self) -> tuple[bool, str | None]:
        return self._inner.health()

    def query(self, user_id: str, *, vector: Any = None, text: Any = None, **kw: Any) -> Any:
        if self._mode == "lexical":
            vector = None
        if self._mode == "dense":
            text = None
        return self._inner.query(user_id, vector=vector, text=text, **kw)


def _load(data: dict[str, Any], embedder: Any, root: Path) -> tuple[SqliteStateStore, SqliteMemoryStore]:
    clock = FixedClock(T0)
    state = SqliteStateStore(str(root / "eval.sqlite"), clock=clock)
    index = SqliteMemoryStore(":memory:")

    for s in data["sources"]:
        versions = range(1, s["current_version"] + 1)
        source = SourceArtifact(
            id=s["id"],
            user_id=s["user"],
            created_at=T0,
            updated_at=T0,
            kind=SourceKind.TEXT,
            lifecycle=SourceLifecycle.READY,
            capture_timezone="America/New_York",
            captured_at=T0,
            current_version=s["current_version"],
            transcripts=[TranscriptRevision(source_version=v, text=s["id"], created_at=T0) for v in versions],
        )
        with state.transaction(s["user"]) as tx:
            tx.put("source", source)

    memories = data["memories"]
    vectors = embedder.embed([m["content"] for m in memories])
    for i, (m, vector) in enumerate(zip(memories, vectors, strict=True)):
        at = T0 + timedelta(minutes=i)  # stable, distinct effective times
        memory = Memory(
            id=m["id"],
            user_id=m["user"],
            created_at=at,
            updated_at=at,
            kind=MemoryKind(m["kind"]),
            content=m["content"],
            project_id=m.get("project"),
            validity=MemoryValidity(m.get("validity", "current")),
            effective_at=at,
            source_refs=[SourceRef(source_id=m["source"], source_version=m.get("source_version", 1))],
            content_hash=content_hash(m["content"]),
            embedding_model_id=embedder.model_id,
            embedding_dimensions=len(vector),
        )
        if not m.get("deleted"):  # deleted = still in the index, gone from state
            with state.transaction(m["user"]) as tx:
                memory = tx.put("memory", memory)
        index.upsert(m["user"], [memory], [vector])
    return state, index


def _score(
    q: dict[str, Any], hits: list[tuple[Memory, float]], state: SqliteStateStore, user: str
) -> dict[str, Any]:
    ids = [m.id for m, _ in hits]
    top = ids[:EVIDENCE]
    expect = q.get("expect", [])
    row: dict[str, Any] = {"id": q["id"], "top": top}

    if expect:
        row["recall@20"] = len(set(expect) & set(ids)) / len(expect)
        row["recall@6"] = len(set(expect) & set(top)) / len(expect)
        first = next((i for i, mid in enumerate(ids) if mid in expect), None)
        row["mrr"] = 0.0 if first is None else 1.0 / (first + 1)
        stale_ranks = [ids.index(s) for s in q.get("stale", []) if s in ids]
        row["stale_first"] = int(bool(stale_ranks) and (first is None or min(stale_ranks) < first))
    row["forbidden"] = len(set(q.get("forbid", [])) & set(ids))
    if q.get("expect_empty"):
        row["false_hits"] = int(bool(top))
    if q.get("expect_projects"):
        seen = {m.project_id for m, _ in hits[:EVIDENCE]}
        row["ambiguity_seen"] = int(set(q["expect_projects"]) <= seen)

    row["leaks"] = sum(m.user_id != user for m, _ in hits)
    bad = 0
    for m, _ in hits:
        for ref in m.source_refs:
            source = state.get(user, "source", ref.source_id)
            bad += source is None or ref.source_version > source.current_version
    row["bad_links"] = bad
    return row


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [r[key] for r in rows if key in r]
    return statistics.fmean(vals) if vals else None


def run(data: dict[str, Any], embedder: Any) -> dict[str, Any]:
    """Return ``{mode: {"rows": [...], "latency_ms": [...]}}``."""
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        state, index = _load(data, embedder, Path(tmp))
        for mode in MODES:
            service = RetrievalService(
                state_store=cast(Any, state),
                memory_store=cast(Any, _ModeStore(index, mode)),
                embedder=embedder,
                clock=FixedClock(T0),
            )
            rows, latency = [], []
            for q in data["queries"]:
                user = q.get("user", "user_a")
                start = time.perf_counter()
                hits, degraded = service.retrieve_supporting_memories(
                    user, query=q["query"], project_id=q.get("project"), limit=CANDIDATES
                )
                latency.append((time.perf_counter() - start) * 1000)
                if degraded:
                    raise RuntimeError(f"{q['id']} ({mode}): retrieval degraded: {degraded}")
                row = _score(q, hits, state, user)
                row.update(split=q["split"], category=q["category"])
                rows.append(row)
            results[mode] = {"rows": rows, "latency_ms": latency}
    return results


def summarize(results: dict[str, Any], split: str | None = None) -> dict[str, dict[str, float | None]]:
    summary: dict[str, dict[str, float | None]] = {}
    for mode, res in results.items():
        rows = [r for r in res["rows"] if split is None or r["split"] == split]
        summary[mode] = {
            "recall@20": _mean(rows, "recall@20"),
            "recall@6": _mean(rows, "recall@6"),
            "mrr": _mean(rows, "mrr"),
            "stale_first": sum(r.get("stale_first", 0) for r in rows),
            "false_hits": sum(r.get("false_hits", 0) for r in rows),
            "ambiguity_seen": _mean(rows, "ambiguity_seen"),
            "forbidden": sum(r["forbidden"] for r in rows),
            "leaks": sum(r["leaks"] for r in rows),
            "bad_links": sum(r["bad_links"] for r in rows),
            "p50_ms": statistics.median(res["latency_ms"]),
        }
    return summary


def _print(summary: dict[str, dict[str, float | None]], title: str) -> None:
    cols = list(next(iter(summary.values())))
    print(f"\n{title}")
    print(f"{'mode':<8}" + "".join(f"{c:>15}" for c in cols))
    for mode, metrics in summary.items():
        cells = [
            "-" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v)) for v in metrics.values()
        ]
        print(f"{mode:<8}" + "".join(f"{c:>15}" for c in cells))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--embedder", choices=("fixture", "configured"), default="fixture")
    parser.add_argument("--failures", action="store_true", help="list queries that missed in the hybrid arm")
    parser.add_argument("--json", type=Path, help="write per-query rows here")
    args = parser.parse_args(argv)

    data = json.loads(args.fixture.read_text())
    embedder = FixtureEmbedder() if args.embedder == "fixture" else build_embedder()
    results = run(data, embedder)
    print(
        f"embedder: {embedder.model_id}  queries: {len(data['queries'])}  memories: {len(data['memories'])}"
    )
    if args.embedder == "fixture":
        print("fixture embedder: dense scores are not a quality signal")
    for split in ("dev", "heldout"):
        _print(summarize(results, split), f"[{split}]")

    if args.failures:
        print("\nhybrid misses (recall@6 < 1, false hit, or ambiguity not shown):")
        for r in results["hybrid"]["rows"]:
            if r.get("recall@6", 1) < 1 or r.get("false_hits") or r.get("ambiguity_seen") == 0:
                print(f"  {r['id']} {r['category']:<20} top={r['top']}")
    if args.json:
        args.json.write_text(json.dumps(results, indent=2))

    hard = summarize(results)
    broken = [f"{mode}.{k}={v}" for mode, m in hard.items() for k, v in m.items() if k in HARD_METRICS and v]
    if broken:
        print("\nFAIL safety metrics:", ", ".join(broken))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
