"""Memory ingestion: typed records -> short memory units -> vectors.

Chunking rules that matter more than the search algorithm:

* one unit per *meaning* (observation, attempt, decision, blocker, idea,
  intention), never one per JSON property
* enough surrounding context to preserve negation, conditionals and the
  difference between an idea and a decision — if "we abandoned this" is lost by
  chunking, the fix belongs here, not in the ranker
* embeddings computed only for new or materially changed text, keyed by content
  hash plus model id and dimension
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from metis_contracts import (
    Memory,
    MemoryKind,
    MemoryValidity,
    SourceArtifact,
    SourceRef,
)
from metis_contracts.enums import ExtractedItemKind

from .embedder import content_hash

log = logging.getLogger("metis.retrieval.ingestion")

#: Extraction item kinds -> memory kinds. Items with no mapping create no memory.
KIND_MAP: dict[ExtractedItemKind, MemoryKind] = {
    ExtractedItemKind.THOUGHT: MemoryKind.IDEA,
    ExtractedItemKind.DECISION: MemoryKind.DECISION,
    ExtractedItemKind.WORK_LOG: MemoryKind.WORK_LOG,
    ExtractedItemKind.TASK: MemoryKind.COMMITMENT,
    ExtractedItemKind.EVENT_INTENT: MemoryKind.IDEA,
    ExtractedItemKind.PREFERENCE: MemoryKind.PREFERENCE,
}

#: Phrases that must survive chunking. Their presence widens the window so a
#: unit never loses the clause that reverses its meaning.
MEANING_CARRIERS = (
    "not",
    "no",
    "never",
    "without",
    "abandoned",
    "failed",
    "stale",
    "instead",
    "but",
    "unless",
    "stopped",
    "gave up",
    "doesn't",
    "don't",
    "won't",
)
_CARRIER_RE = re.compile(r"\b(?:" + "|".join(re.escape(c) for c in MEANING_CARRIERS) + r")\b", re.IGNORECASE)


def context_window(text: str, *, start: int, end: int, pad: int = 160) -> str:
    """Widen a span to its sentence-ish neighbourhood.

    The window is widened further when a nearby clause carries negation or an
    abandonment marker, because dropping it would invert the meaning.
    """
    lo, hi = max(0, start - pad), min(len(text), end + pad)
    # Whole words only, and only near the span: "no" must not match "know",
    # and a negation elsewhere in a long note says nothing about this span.
    if _CARRIER_RE.search(text[lo:hi]):
        lo, hi = max(0, start - pad * 2), min(len(text), end + pad * 2)
    return " ".join(text[lo:hi].split())


def memory_content_for(item: Any, source: SourceArtifact) -> str:
    """Memory text for one extracted item: the span's context window when the
    item cites a span of ``source``, else its title + body."""
    text = source.current_text() or ""
    ref = item.evidence[0] if getattr(item, "evidence", None) else None
    span = next((s for s in source.spans if ref is not None and s.span_ref == ref.span_ref), None)
    if span is not None and text:
        return context_window(text, start=span.start_char, end=span.end_char)
    return " ".join(filter(None, [item.title, item.body or ""]))


CHUNK_SIZE = 2000  # characters (~500 tokens)
CHUNK_OVERLAP = 240  # characters (~60 tokens)


@dataclass(frozen=True)
class Chunk:
    start_char: int
    end_char: int
    text: str
    """Verbatim slice ``extracted[start_char:end_char]``: what citations quote."""
    embed_text: str
    """``text`` prefixed with its context line, so a chunk stands alone."""

    @property
    def span_ref(self) -> str:
        return f"chunk:{self.start_char}-{self.end_char}"


def chunk_text(text: str, *, title: str, path: str) -> list[Chunk]:
    """Structural split (paragraphs, lines, words) packed to ~2000 chars."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, add_start_index=True
    )
    context = f"{title} › {path}"  # noqa: RUF001 - the spec's context separator
    chunks: list[Chunk] = []
    for doc in splitter.create_documents([text]):
        start = int(doc.metadata.get("start_index", -1))
        if start < 0 or text[start : start + len(doc.page_content)] != doc.page_content:
            continue  # the splitter could not locate its own slice; never cite a guessed span
        end = start + len(doc.page_content)
        chunks.append(Chunk(start, end, doc.page_content, f"{context}\n{doc.page_content}"))
    return chunks


def memories_from_proposal(
    proposal: Any, source: SourceArtifact, *, clock: Any, embedding_model_id: str, dimensions: int
) -> list[Memory]:
    """Build one memory unit per meaningful extracted item."""
    now = clock.now()
    memories: list[Memory] = []

    for item in proposal.items:
        kind = KIND_MAP.get(item.kind)
        if kind is None:
            continue
        ref = item.evidence[0] if item.evidence else None
        if ref is None:
            continue  # a memory without a source cannot be evidence
        content = memory_content_for(item, source)[:8000]
        if not content.strip():
            continue
        mem_id = memory_id_for(source.user_id, content_hash(content))
        if any(m.id == mem_id for m in memories):
            continue  # same text twice in one proposal is one memory
        memories.append(
            Memory(
                id=mem_id,
                user_id=source.user_id,
                created_at=now,
                updated_at=now,
                kind=kind,
                content=content,
                project_id=None,
                validity=MemoryValidity.CURRENT,
                effective_at=source.captured_at,
                source_refs=[
                    SourceRef(
                        source_id=ref.source_id,
                        source_version=ref.source_version,
                        span_ref=ref.span_ref,
                    )
                ],
                content_hash=content_hash(content),
                embedding_model_id=embedding_model_id,
                embedding_dimensions=dimensions,
                indexed_source_version=ref.source_version,
            )
        )
    return memories


def memory_id_for(user_id: str, content_hash: str) -> str:
    """One memory per owner per distinct content. Identical text maps to the
    same id, so it is stored, queued and indexed once, never twice."""
    return "mem_" + hashlib.sha256(f"{user_id}:{content_hash}".encode()).hexdigest()[:24]


def needs_embedding(memory: Memory, known_hashes: set[str]) -> bool:
    """Reuse an unchanged embedding rather than paying for it again."""
    return memory.content_hash not in known_hashes


def mark_superseded(previous: Memory, *, clock: Any) -> Memory:
    """Supersede rather than delete: past alternatives stay inspectable.

    A high similarity score must not keep a superseded version in play as if it
    were current; retrieval drops non-current memories unless history is asked for.
    """
    return previous.model_copy(update={"validity": MemoryValidity.SUPERSEDED, "updated_at": clock.now()})
