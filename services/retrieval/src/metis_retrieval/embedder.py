"""Embedder adapters.

Embeddings are reused aggressively: they are keyed by a content hash plus the
model id and dimension, so unchanged text is never re-embedded and an index
written with one model is never queried with another.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
from collections.abc import Sequence
from typing import Any

from langchain_core.embeddings import Embeddings
from metis_contracts import AdapterMode, ErrorCode, MetisError, settings
from metis_pipeline.local_llm import LocalConfig, inference_engine

log = logging.getLogger("metis.retrieval.embedder")


def content_hash(text: str) -> str:
    """Stable key for embedding reuse. Normalised so whitespace edits do not
    invalidate a cached vector."""
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def normalize(vector: list[float]) -> list[float]:
    magnitude = math.sqrt(sum(v * v for v in vector))
    return [v / magnitude for v in vector] if magnitude else vector


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        # A dimension mismatch means the index was written with another model.
        raise MetisError(
            ErrorCode.VALIDATION_FAILED,
            f"embedding dimension mismatch: {len(a)} vs {len(b)}; reindex before querying",
        )
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class _Embedder(Embeddings):
    """LangChain ``Embeddings`` over our ``embed`` port (D7).

    ``semantic`` says honestly whether vectors carry meaning: a fixture hash
    does not, and retrieval then runs keyword-only and says so.
    """

    semantic = True
    model_id: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


class FixtureEmbedder(_Embedder):
    """Deterministic hashed embedding for local mode and tests.

    Not semantically meaningful across paraphrases — it is a stand-in so the
    indexing, filtering, eviction and evidence-hydration paths are all
    exercisable offline. Lexical retrieval carries the local demo's recall.
    """

    mode = AdapterMode.FIXTURE
    model_id = "fixture-embed-v1"
    semantic = False

    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            # Hashed bag of words: shared vocabulary produces similarity, which
            # is enough to exercise ranking without pretending to be a model.
            vector = [0.0] * self.dimensions
            for token in " ".join(text.split()).lower().split():
                digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
                index = struct.unpack("<Q", digest)[0] % self.dimensions
                vector[index] += 1.0
            vectors.append(normalize(vector))
        return vectors


class LocalEmbedder(_Embedder):
    """Ollama embeddings over loopback via ``langchain_ollama.OllamaEmbeddings``.

    Dimension is learned from the first response and checked on every later
    one, so a model swap cannot silently mix vector spaces."""

    mode = AdapterMode.LOCAL
    MAX_BATCH = 32

    def __init__(self, config: LocalConfig | None = None) -> None:
        from langchain_ollama import OllamaEmbeddings

        cfg = config or LocalConfig.from_env()
        if not cfg.embed_model:
            raise MetisError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "METIS_LOCAL_EMBED_MODEL is not set; choose a locally installed model explicitly",
            )
        self.model_id = cfg.embed_model
        self._dimensions: int | None = None
        # trust_env=False: a loopback call must never be routed through a proxy.
        self._client = OllamaEmbeddings(
            model=cfg.embed_model,
            base_url=cfg.endpoint,
            client_kwargs={"timeout": settings().limits.model_timeout_seconds, "trust_env": False},
        )

    @property
    def dimensions(self) -> int:
        if self._dimensions is None:
            self.embed(["dimension probe"])
        assert self._dimensions is not None
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.MAX_BATCH):
            batch = list(texts[i : i + self.MAX_BATCH])
            try:
                got = self._client.embed_documents(batch)
            except Exception as exc:
                # Ollama down or model not pulled: transient, retried by the outbox.
                raise MetisError(
                    ErrorCode.PROVIDER_UNAVAILABLE, f"local embedding call failed: {exc}"[:500]
                ) from exc
            if not isinstance(got, list) or len(got) != len(batch):
                raise MetisError(
                    ErrorCode.VALIDATION_FAILED, "local embedding model returned the wrong vector count"
                )
            for vec in got:
                if not isinstance(vec, Sequence) or not vec:
                    raise MetisError(ErrorCode.VALIDATION_FAILED, "local embedding vector is empty")
                if self._dimensions is None:
                    self._dimensions = len(vec)
                elif len(vec) != self._dimensions:
                    raise MetisError(
                        ErrorCode.VALIDATION_FAILED,
                        f"embedding dimension changed: {len(vec)} vs {self._dimensions}",
                    )
                vectors.append(normalize([float(v) for v in vec]))
        return vectors


EMBED_ENGINES = ("auto", "fixture", "local")


def embed_engine() -> str:
    """``METIS_EMBED_ENGINE``, decoupled from the LLM engine (spec §5.4).

    ``auto``: local when ``METIS_LOCAL_EMBED_MODEL`` is set, else follow
    ``METIS_INFERENCE_ENGINE`` as before.
    """
    engine = (os.environ.get("METIS_EMBED_ENGINE") or "auto").strip().lower()
    if engine not in EMBED_ENGINES:
        raise ValueError(f"METIS_EMBED_ENGINE must be one of {EMBED_ENGINES}; got {engine!r}")
    if engine == "auto":
        return "local" if os.environ.get("METIS_LOCAL_EMBED_MODEL") else inference_engine()
    return engine


def semantic_available(embedder: Any) -> bool:
    """True only when vectors come from a real model, never a fixture."""
    return (
        embedder is not None
        and getattr(embedder, "mode", None) is not AdapterMode.FIXTURE
        and getattr(embedder, "semantic", True)
    )


def build_embedder() -> Any:
    engine = embed_engine()
    if engine == "local":
        return LocalEmbedder()
    return FixtureEmbedder()
