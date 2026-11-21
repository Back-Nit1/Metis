"""Optional, final-only dictation cleanup through a loopback Ollama runtime.

The recognizer output remains authoritative.  This module may produce a more
readable derivative for insertion, but it never replaces ``raw_text`` and it
never runs on provisional hypotheses.  Any runtime, schema, or semantic guard
failure falls back to the deterministic polisher.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

MAX_INPUT_CHARS = 24_000
MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT_SECONDS = 20.0

_NUMBER = re.compile(r"(?<!\w)\d+(?::\d+)?(?:[./-]\d+)*(?:am|pm)?(?!\w)", re.IGNORECASE)
_NEGATION = re.compile(
    r"\b(?:no|not|never|neither|nor|cannot|can't|won't|don't|doesn't|didn't|isn't|aren't|wasn't|"
    r"weren't|shouldn't|wouldn't|couldn't|mustn't)\b",
    re.IGNORECASE,
)
_ADDRESS = re.compile(r"\b(?:https?://|www\.)\S+|\b[\w.+-]+@[\w.-]+\.\w+\b", re.IGNORECASE)

_FORMAT: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

_SYSTEM = """You clean a finalized speech transcript for insertion into the user's focused app.
Remove filler words and abandoned false starts. Apply only explicit self-corrections. Improve punctuation,
paragraphs, and concise structure while preserving the user's meaning and language. Never invent or infer
facts. Never add a date, time, number, name, URL, email address, commitment, task, reminder, or negation.
Return exactly one JSON object matching the supplied schema. The text is a derivative; the original is kept
separately."""


@dataclass(frozen=True)
class ModelPolish:
    text: str
    model_id: str
    input_chars: int


class OllamaPolisher:
    """Bounded Ollama client used only for finalized dictation text."""

    def __init__(
        self,
        model_id: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        parts = urlsplit(endpoint.rstrip("/"))
        if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost") or not parts.port:
            raise ValueError("METIS_FLOW_MODEL_ENDPOINT must be an explicit loopback HTTP endpoint")
        if not model_id.strip():
            raise ValueError("METIS_FLOW_MODEL must name an installed Ollama model")
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("METIS_FLOW_MODEL_TIMEOUT_SECONDS must be between 0 and 60")
        self.model_id = model_id.strip()
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def polish(
        self,
        *,
        raw_text: str,
        deterministic_text: str,
        app: str | None,
        style: str,
        protected_terms: tuple[str, ...] = (),
    ) -> ModelPolish:
        if not deterministic_text:
            return ModelPolish("", self.model_id, 0)
        if len(raw_text) > MAX_INPUT_CHARS:
            raise ValueError(f"final transcript is over the {MAX_INPUT_CHARS}-character cleanup limit")

        user = json.dumps(
            {
                "raw_final_transcript": raw_text,
                "deterministic_cleanup": deterministic_text,
                "focused_app": app,
                "style": style,
                "protected_terms": list(protected_terms),
            },
            ensure_ascii=False,
        )
        request = urllib.request.Request(
            self.endpoint + "/api/chat",
            data=json.dumps(
                {
                    "model": self.model_id,
                    "stream": False,
                    "format": _FORMAT,
                    "messages": [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    "options": {"temperature": 0, "num_predict": 4096},
                }
            ).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("local cleanup model is unavailable") from exc
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("local cleanup model response is too large")

        try:
            reply = json.loads(payload)
            if reply.get("done_reason") == "length":
                raise ValueError("local cleanup model response was truncated")
            content = (reply.get("message") or {}).get("content")
            if not isinstance(content, str):
                raise ValueError("local cleanup model response has no text content")
            result = json.loads(content)
            candidate = result["text"].strip()
        except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("local cleanup model returned an invalid response") from exc
        if not candidate:
            raise ValueError("local cleanup model removed the entire transcript")
        _guard_semantics(raw_text, deterministic_text, candidate, protected_terms)
        return ModelPolish(candidate, self.model_id, len(raw_text))


def from_env() -> OllamaPolisher | None:
    """Build the opt-in cleanup adapter; no model name means deterministic mode."""
    model = (os.environ.get("METIS_FLOW_MODEL") or "").strip()
    if not model:
        return None
    try:
        timeout = float(os.environ.get("METIS_FLOW_MODEL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    except ValueError as exc:
        raise ValueError("METIS_FLOW_MODEL_TIMEOUT_SECONDS must be numeric") from exc
    return OllamaPolisher(
        model,
        endpoint=os.environ.get("METIS_FLOW_MODEL_ENDPOINT", DEFAULT_ENDPOINT),
        timeout_seconds=timeout,
    )


def _guard_semantics(
    raw: str,
    deterministic_text: str,
    candidate: str,
    protected_terms: tuple[str, ...],
) -> None:
    """Reject high-risk meaning drift without pretending to prove equivalence.

    Explicit self-correction can legitimately remove an earlier number or name,
    so those values may be removed but never introduced.  Negation cannot be
    safely dropped or added and therefore must match exactly.
    """

    def normalized(pattern: re.Pattern[str], value: str) -> Counter[str]:
        return Counter(match.group(0).casefold().rstrip(".,;:!?") for match in pattern.finditer(value))

    raw_numbers = normalized(_NUMBER, raw)
    candidate_numbers = normalized(_NUMBER, candidate)
    raw_addresses = normalized(_ADDRESS, raw)
    candidate_addresses = normalized(_ADDRESS, candidate)
    if candidate_numbers - raw_numbers:
        raise ValueError("local cleanup model introduced a number or time")
    if candidate_addresses - raw_addresses:
        raise ValueError("local cleanup model introduced an address")
    if normalized(_NEGATION, raw) != normalized(_NEGATION, candidate):
        raise ValueError("local cleanup model changed negation")

    protected_source = deterministic_text.casefold()
    candidate_folded = candidate.casefold()
    for term in protected_terms:
        if term.casefold() in protected_source and term.casefold() not in candidate_folded:
            raise ValueError("local cleanup model removed a protected term")
