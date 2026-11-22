"""The narrow, server-controlled retrieval operations the planner may call.

Separation of duties, enforced here:

* the authoritative store answers *what is true now* — deadlines, completion,
  cancellation, ownership, versions
* semantic memory supplies *why a task exists and what was tried*

A retrieved memory can never override newer authoritative state, and every
candidate is hydrated against that state before it is admitted as evidence.
Deleted, tombstoned and version-mismatched hits are dropped; a cross-owner hit
is dropped and logged as a bug.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from typing import Any, cast

from langchain_core.documents import Document
from metis_contracts import (
    Event,
    Evidence,
    Memory,
    Project,
    SourceArtifact,
    Task,
    settings,
)
from metis_contracts.api import SearchResult
from metis_contracts.entities import AvailabilityBlock
from metis_contracts.enums import TASK_OPEN_STATES, SourceLifecycle
from metis_contracts.ports import Clock, Embedder, MemoryStore, StateStore

from .corpus_store import ChunkRow
from .decomposition import DecomposedSearch, Decomposer, build_decomposer
from .embedder import semantic_available
from .retrievers import CorpusLane, MemoryLane, lane_retriever

log = logging.getLogger("metis.retrieval.service")


#: Rough token estimate for the shared evidence budget.
def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class ObligationScan:
    """Everything due or open in the horizon, plus whether the scan completed."""

    tasks: list[Task] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    complete: bool = True
    pages_read: int = 0

    @property
    def incomplete_reason(self) -> str | None:
        return None if self.complete else "the obligation scan hit its configured read limit"


@dataclass
class EvidenceBudget:
    """Total evidence budget across the whole plan, not per task.

    Without this, "at most 6 per task" multiplied by a dozen tasks quietly
    becomes an enormous prompt.
    """

    max_tokens: int
    used_tokens: int = 0

    def admit(self, text: str) -> bool:
        cost = _tokens(text)
        if self.used_tokens + cost > self.max_tokens:
            return False
        self.used_tokens += cost
        return True


class RetrievalService:
    """Implements the planner-facing operations and nothing wider."""

    def __init__(
        self,
        *,
        state_store: StateStore,
        memory_store: MemoryStore,
        embedder: Embedder,
        clock: Clock,
        decomposer: Decomposer | None = None,
    ) -> None:
        self._decomposer = decomposer or build_decomposer()
        self._state = state_store
        self._memory = memory_store
        self._embedder = embedder
        self._clock = clock

    # -- authoritative ------------------------------------------------- #

    def list_eligible_obligations(
        self, user_id: str, *, local_date: date, horizon_days: int | None = None
    ) -> ObligationScan:
        """Every near-term obligation, found by structured query — not by similarity.

        A task with an explicit date must appear even when it is semantically
        unrelated to the planning request, so this never consults the vector
        store. Pagination is bounded; hitting the bound sets ``complete=False``
        and the plan may then not claim full coverage.
        """
        horizon = local_date + timedelta(days=horizon_days or settings().limits.planning_horizon_days)
        page_limit = settings().limits.planning_read_page_limit
        scan = ObligationScan()

        cursor: str | None = None
        while scan.pages_read < page_limit:
            page = self._state.list_tasks(user_id, cursor=cursor, limit=100)
            scan.pages_read += 1
            scan.tasks.extend(t for t in page.items if t.state in TASK_OPEN_STATES)
            if not page.next_cursor:
                break
            cursor = page.next_cursor
        else:
            scan.complete = False

        cursor = None
        while scan.pages_read < page_limit * 2:
            event_page = self._state.list_events(user_id, cursor=cursor, limit=100)
            scan.pages_read += 1
            scan.events.extend(
                e
                for e in event_page.items
                if e.attendance_status.value not in ("canceled", "declined")
                and (e.local_date is None or e.local_date <= horizon)
            )
            if not event_page.next_cursor:
                break
            cursor = event_page.next_cursor
        else:
            scan.complete = False

        return scan

    def get_current_task_state(self, user_id: str, task_id: str) -> Task | None:
        task = self._state.get(user_id, "task", task_id)
        return task if isinstance(task, Task) else None

    def get_availability(self, user_id: str, local_date: date) -> list[AvailabilityBlock]:
        return self._state.list_availability(user_id, local_date)

    def lookup_identifier(self, user_id: str, identifier: str) -> list[Project]:
        """Literal lookup for project names, repo names and error codes.

        Aliases are matched directly because ordinary tokenisation — and a
        paraphrase-tuned embedding — both lose exact identifiers.
        """
        needle = identifier.strip().casefold()
        if not needle:
            return []
        matches: list[Project] = []
        cursor: str | None = None
        lister = getattr(self._state, "list_projects", None)
        while lister is not None:
            page = lister(user_id, cursor=cursor, limit=100)
            for project in page.items:
                names = [project.title, *project.aliases]
                if any(needle == n.strip().casefold() for n in names):
                    matches.insert(0, project)  # exact match ranks first
                elif any(needle in n.casefold() for n in names):
                    matches.append(project)
            if not page.next_cursor:
                break
            cursor = page.next_cursor
        return matches

    # -- semantic ------------------------------------------------------- #

    def get_project_history(self, user_id: str, project_id: str, *, limit: int = 20) -> list[Memory]:
        """Chronological project history, read directly rather than searched.

        When the project identity is already known, a similarity search is
        strictly worse than reading its own records.
        """
        page = self._state.list_memories(user_id, project_id=project_id, limit=limit)
        return sorted(page.items, key=lambda m: m.effective_at, reverse=True)

    def retrieve_supporting_memories(
        self,
        user_id: str,
        *,
        query: str,
        project_id: str | None = None,
        limit: int | None = None,
        include_historical: bool = True,
    ) -> tuple[list[tuple[Memory, float]], str | None]:
        """Hybrid memory-lane retrieval, hydrated against authoritative state.

        ``include_historical`` defaults to True because a failed attempt is
        exactly the evidence that explains a blocker — filtering all history
        away would make Metis re-suggest an approach the user already tried.
        The second element is set only when the memory index is unavailable or
        failed; keyword-only operation is reported by ``search``.
        """
        hits, unavailable, _ = self._hybrid(
            user_id,
            query=query,
            project_id=project_id,
            limit=limit or settings().limits.retrieval_candidates,
            include_historical=include_historical,
            origins=("memory",),
        )
        return [(h.memory, h.score) for h in hits if h.memory is not None], unavailable

    def search(
        self,
        user_id: str,
        *,
        query: str,
        project_id: str | None = None,
        limit: int = 20,
        include_historical: bool = False,
        origins: Sequence[str] = (),
    ) -> tuple[list[SearchResult], str | None]:
        """Hybrid (semantic + keyword) search over memories and the corpus.

        Every result carries a citation in ``evidence``. A project scope limits
        the search to that project's memories: corpus chunks have no project.
        The second element explains any degradation (index down, keyword only).
        """
        if project_id is not None:
            origins = ("memory",)
        hits, unavailable, keyword_only = self._hybrid(
            user_id,
            query=query,
            project_id=project_id,
            limit=limit,
            include_historical=include_historical,
            origins=origins,
        )
        results = [
            SearchResult(
                memory_id=h.memory.id if h.memory else None,
                chunk_id=h.chunk.chunk_id if h.chunk else None,
                kind=h.memory.kind if h.memory else None,
                content=h.memory.content if h.memory else h.chunk.content if h.chunk else "",
                score=h.score,
                evidence=h.evidence(),
                project_id=h.memory.project_id if h.memory else None,
            )
            for h in hits
        ]
        reasons = [r for r in (unavailable, keyword_only) if r]
        return results, "; ".join(reasons) or None

    def search_decomposed(
        self,
        user_id: str,
        *,
        query: str,
        project_id: str | None = None,
        limit: int = 20,
        include_historical: bool = False,
    ) -> DecomposedSearch:
        """``search``, split into sub-queries when a local planner is enabled."""

        def run(text: str) -> tuple[list[SearchResult], str | None]:
            return self.search(
                user_id,
                query=text,
                project_id=project_id,
                limit=limit,
                include_historical=include_historical,
            )

        if self._decomposer is None:
            return DecomposedSearch(*run(query))
        return self._decomposer.search(query, run, limit=limit)

    def _hybrid(
        self,
        user_id: str,
        *,
        query: str,
        project_id: str | None,
        limit: int,
        include_historical: bool,
        origins: Sequence[str],
    ) -> tuple[list[_Hit], str | None, str | None]:
        """Fuse semantic + keyword per lane (RRF), cap the corpus share, hydrate.

        Returns ``(hits, unavailable_reason, keyword_only_reason)``.
        """
        want_memory = not origins or "memory" in origins
        corpus_origins = tuple(o for o in origins if o != "memory")
        corpus = getattr(self._memory, "corpus", None)
        want_corpus = corpus is not None and (not origins or bool(corpus_origins))

        embed, keyword_only = self._query_embedder(query)
        unavailable: str | None = None
        memory_docs: list[Document] = []
        corpus_docs: list[Document] = []

        if want_memory:
            available, degraded = self._memory.health()
            if not available:
                # Planning and reminders must keep working without the index.
                unavailable = degraded or "the memory index is unavailable"
            else:
                lane = MemoryLane(self._memory, user_id, project_id, include_historical)
                retriever = lane_retriever(
                    lane, k=limit, embed=embed, keyword=bool(getattr(self._memory, "supports_lexical", False))
                )
                try:
                    memory_docs = retriever.invoke(query) if retriever else []
                except Exception as exc:
                    unavailable = f"retrieval degraded: {exc}"
        if want_corpus:
            lane_c = CorpusLane(corpus, user_id, getattr(self._embedder, "model_id", ""), corpus_origins)
            retriever = lane_retriever(lane_c, k=limit, embed=embed)
            try:
                corpus_docs = retriever.invoke(query) if retriever else []
            except Exception as exc:
                unavailable = "; ".join(filter(None, [unavailable, f"corpus retrieval degraded: {exc}"]))

        memory_hits = self._hydrate_memories(user_id, memory_docs)
        corpus_hits = self._hydrate_chunks(user_id, corpus, corpus_docs)
        if want_memory:
            # Memories are first-class; imported content takes at most its share.
            share = settings().limits.retrieval_corpus_share_pct
            corpus_hits = corpus_hits[: max(1, limit * share // 100)]
        hits = sorted([*memory_hits, *corpus_hits], key=lambda h: h.score, reverse=True)[:limit]
        return hits, unavailable, keyword_only

    def _query_embedder(self, query: str) -> tuple[Callable[[str], Sequence[float]] | None, str | None]:
        """A memoised query embedding shared by both lanes, or keyword-only + why."""
        if not semantic_available(self._embedder):
            return None, "keyword search only: no semantic embedding model is configured"
        embedder = self._embedder

        @lru_cache(maxsize=4)
        def embed(text: str) -> tuple[float, ...]:
            return tuple(embedder.embed([text])[0])

        try:
            embed(query)
        except Exception as exc:
            log.warning("embedding the query failed: %s", exc)
            return None, f"keyword search only: the embedding model is unavailable ({exc})"
        return embed, None

    def _hydrate_memories(self, user_id: str, docs: list[Document]) -> list[_Hit]:
        hits: list[_Hit] = []
        stale = 0
        for doc in docs:
            memory_id = doc.metadata["id"]
            memory = self._state.get(user_id, "memory", memory_id)
            if memory is None:
                stale += 1  # deleted or tombstoned since indexing
                continue
            if memory.user_id != user_id:
                log.error("memory %s belongs to another owner; dropped before any prompt", memory_id)
                continue
            if not self._source_is_current(user_id, memory):
                stale += 1
                continue
            hits.append(_Hit(score=doc.metadata["score"], matched=doc.metadata["matched"], memory=memory))
        if stale:
            log.info("dropped %d stale or inaccessible memory candidates", stale)
        return hits

    def _hydrate_chunks(self, user_id: str, corpus: Any, docs: list[Document]) -> list[_Hit]:
        """A chunk is evidence only while its source exists, is live and is at
        the chunk's version."""
        if not docs:
            return []
        rows = corpus.get(user_id, [d.metadata["id"] for d in docs])
        hits: list[_Hit] = []
        for doc in docs:
            chunk = rows.get(doc.metadata["id"])
            if chunk is None:
                continue
            if chunk.user_id != user_id:
                log.error("chunk %s belongs to another owner; dropped", chunk.chunk_id)
                continue
            source = self._state.get(user_id, "source", chunk.source_id)
            if (
                source is None
                or source.user_id != user_id
                or source.tombstoned_at is not None
                or source.lifecycle is SourceLifecycle.TOMBSTONED
                or source.current_version != chunk.source_version
            ):
                continue
            hits.append(
                _Hit(score=doc.metadata["score"], matched=doc.metadata["matched"], chunk=chunk, source=source)
            )
        return hits

    def _source_is_current(self, user_id: str, memory: Memory) -> bool:
        """A memory indexed from an older source revision is not evidence for now."""
        for ref in memory.source_refs:
            source = self._state.get(user_id, "source", ref.source_id)
            if source is None:
                return False
            if ref.source_version < source.current_version and memory.validity.value == "current":
                return False
        return True

    # -- evidence assembly ----------------------------------------------- #

    def evidence_for_task(
        self,
        user_id: str,
        task: Task,
        *,
        budget: EvidenceBudget,
        per_task: int | None = None,
    ) -> list[Evidence]:
        """At most ``per_task`` items, within the plan-wide token budget.

        Related spans from the same source are deduplicated so one note cannot
        occupy the whole budget.
        """
        cap = per_task or settings().limits.retrieval_evidence_per_task
        query = " ".join(filter(None, [task.title, task.body or ""]))[:500]
        hits, degraded, _ = self._hybrid(
            user_id,
            query=query,
            project_id=task.project_id,
            limit=settings().limits.retrieval_candidates,
            include_historical=True,
            origins=(),
        )
        if degraded and not hits:
            log.info("no evidence for task %s: %s", task.id, degraded)
            return []

        seen_sources: set[str] = set()
        evidence: list[Evidence] = []
        for hit in hits:
            if len(evidence) >= cap:
                break
            item = hit.evidence()
            if item is None:
                continue
            key = f"{item.source_id}:{item.source_version}"
            if key in seen_sources:
                continue
            if not budget.admit(item.excerpt):
                break
            seen_sources.add(key)
            evidence.append(item)
        return evidence


@dataclass
class _Hit:
    """One hydrated candidate from either lane."""

    score: float
    matched: list[Any]
    memory: Memory | None = None
    chunk: ChunkRow | None = None
    source: SourceArtifact | None = None

    def evidence(self) -> Any:
        """The citation. Corpus excerpts are labelled by origin so a file or
        email is never presented as the user's own decision."""
        if self.memory is not None:
            ref = self.memory.source_refs[0] if self.memory.source_refs else None
            if ref is None:
                return None
            return Evidence(
                source_id=ref.source_id,
                source_version=ref.source_version,
                span_ref=ref.span_ref,
                memory_id=self.memory.id,
                excerpt=self.memory.content[:2000],
                recorded_at=self.memory.effective_at,
                # A failed attempt stays retrievable, labelled historical.
                validity="historical"
                if self.memory.kind.value in ("attempt", "work_log")
                else self.memory.validity.value,
                origin="memory",
                matched=self.matched,
            )
        assert self.chunk is not None and self.source is not None
        chunk = self.chunk
        return Evidence(
            source_id=chunk.source_id,
            source_version=chunk.source_version,
            span_ref=f"chunk:{chunk.start_char}-{chunk.end_char}",
            excerpt=chunk.content[:2000],
            recorded_at=(self.source.origin.modified_at if self.source.origin else None)
            or self.source.captured_at,
            origin=cast(Any, chunk.origin if chunk.origin in ("file", "email", "drive") else "file"),
            chunk_id=chunk.chunk_id,
            title=chunk.title[:500],
            uri=chunk.uri[:2000],
            start_char=chunk.start_char,
            end_char=chunk.end_char,
            matched=self.matched,
        )
