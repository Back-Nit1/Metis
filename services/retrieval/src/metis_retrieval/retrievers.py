"""LangChain retrievers over the memory and corpus lanes (D7, D8).

``KeywordRetriever`` (FTS5 BM25) and ``SemanticRetriever`` (exact cosine) each
rank one lane; ``HybridRetriever`` fuses them with the weighted reciprocal
rank fusion ``EnsembleRetriever`` uses: ``score = sum(w_i / (rank_i + c))``,
``c = 60``, rank 1-based. Every fused document records which matchers hit in
``metadata["matched"]``.

Documents carry ids only (``metadata["key"]``); the retrieval service hydrates
each against authoritative state before any text is used.

Privacy: corpus text must never leave the machine. LangSmith tracing is forced
off below, so an inherited ``LANGSMITH_TRACING=true`` cannot export it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

for _var in ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2"):
    os.environ[_var] = "false"

RRF_C = 60
Matcher = Literal["semantic", "keyword"]


class Lane(Protocol):
    name: str

    def keyword(self, query: str, k: int) -> list[str]: ...

    def semantic(self, vector: Sequence[float], k: int) -> list[str]: ...


def _docs(lane: str, ids: list[str], matcher: Matcher) -> list[Document]:
    return [
        Document(page_content="", metadata={"key": f"{lane}:{i}", "lane": lane, "id": i, "matcher": matcher})
        for i in ids
    ]


class KeywordRetriever(BaseRetriever):
    lane: Any
    k: int = 20

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        return _docs(self.lane.name, self.lane.keyword(query, self.k), "keyword")


class SemanticRetriever(BaseRetriever):
    lane: Any
    embed: Callable[[str], Sequence[float]]
    """Query embedding; the service passes a memoised one so lanes share a call."""
    k: int = 20

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        return _docs(self.lane.name, self.lane.semantic(self.embed(query), self.k), "semantic")


class HybridRetriever(BaseRetriever):
    retrievers: list[BaseRetriever]
    weights: list[float] | None = None
    c: int = RRF_C

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        weights = self.weights or [1 / len(self.retrievers)] * len(self.retrievers)
        fused: dict[str, Document] = {}
        for retriever, weight in zip(self.retrievers, weights, strict=True):
            docs = retriever.invoke(query, config={"callbacks": run_manager.get_child()})
            for rank, doc in enumerate(docs, start=1):
                key = doc.metadata["key"]
                hit = fused.setdefault(
                    key,
                    Document(
                        page_content=doc.page_content, metadata={**doc.metadata, "score": 0.0, "matched": []}
                    ),
                )
                hit.metadata["score"] += weight / (rank + self.c)
                if doc.metadata["matcher"] not in hit.metadata["matched"]:
                    hit.metadata["matched"].append(doc.metadata["matcher"])
        for hit in fused.values():
            hit.metadata.pop("matcher", None)
            hit.metadata["matched"].sort(key=["semantic", "keyword"].index)
        return sorted(fused.values(), key=lambda d: d.metadata["score"], reverse=True)


@dataclass
class MemoryLane:
    store: Any
    user_id: str
    project_id: str | None
    include_historical: bool
    name: str = "memory"

    def keyword(self, query: str, k: int) -> list[str]:
        hits = self.store.query(
            self.user_id,
            vector=None,
            text=query,
            project_id=self.project_id,
            include_historical=self.include_historical,
            limit=k,
        )
        return [h[0] for h in hits]

    def semantic(self, vector: Sequence[float], k: int) -> list[str]:
        hits = self.store.query(
            self.user_id,
            vector=vector,
            text=None,
            project_id=self.project_id,
            include_historical=self.include_historical,
            limit=k,
        )
        return [h[0] for h in hits]


@dataclass
class CorpusLane:
    store: Any
    user_id: str
    model_id: str
    origins: Sequence[str] = ()
    name: str = "corpus"

    def keyword(self, query: str, k: int) -> list[str]:
        return list(self.store.keyword(self.user_id, query, limit=k, origins=self.origins))

    def semantic(self, vector: Sequence[float], k: int) -> list[str]:
        hits = self.store.semantic(
            self.user_id, vector, model_id=self.model_id, limit=k, origins=self.origins
        )
        return [h[0] for h in hits]


def lane_retriever(
    lane: Lane, *, k: int, embed: Callable[[str], Sequence[float]] | None, keyword: bool = True
) -> HybridRetriever | None:
    """Keyword and (when real embeddings exist) semantic, fused within one lane."""
    parts: list[BaseRetriever] = []
    if embed is not None:
        parts.append(SemanticRetriever(lane=lane, embed=embed, k=k))
    if keyword:
        parts.append(KeywordRetriever(lane=lane, k=k))
    return HybridRetriever(retrievers=parts) if parts else None
