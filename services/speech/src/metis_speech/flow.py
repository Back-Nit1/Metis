"""Hold-to-dictate sessions for the warm speech server.

Audio is streamed as raw PCM16 mono 16 kHz chunks while the key is held and
buffered in memory only. Partials are provisional hints for the HUD; the final
text exists only after ``finish`` and is the only thing a client may insert.
Nothing here creates tasks, reminders or sources.
"""

from __future__ import annotations

import io
import secrets
import threading
import time
import wave
from dataclasses import dataclass, field, replace

from metis_contracts import ErrorCode, FlowSettings, MetisError

RATE = 16_000
BYTES_PER_SECOND = RATE * 2
PARTIAL_EVERY_SECONDS = 2.0
IDLE_TTL_SECONDS = 120.0


def wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


@dataclass
class Session:
    id: str
    language: str | None
    app: str | None
    cfg: FlowSettings
    owner: str = "local"
    pcm: bytearray = field(default_factory=bytearray)
    partial_at: int = 0  # buffer length when the last partial was decoded
    touched: float = field(default_factory=time.monotonic)
    finishing: bool = False

    @property
    def seconds(self) -> float:
        return len(self.pcm) / BYTES_PER_SECOND

    def partial_due(self) -> bool:
        return len(self.pcm) - self.partial_at >= PARTIAL_EVERY_SECONDS * BYTES_PER_SECOND


class Sessions:
    """One owner-bound session shared by all callers of this speech process.

    Admission, audio mutation and finish/cancel transitions share one lock.
    An idle session expires; a new caller never replaces an active one.
    """

    def __init__(self, max_seconds: int) -> None:
        self._max_bytes = max_seconds * BYTES_PER_SECOND
        self._lock = threading.Lock()
        self._current: Session | None = None

    def create(
        self, language: str | None, app: str | None, cfg: FlowSettings, *, owner: str = "local"
    ) -> Session:
        with self._lock:
            current = self._current
            if current is not None and time.monotonic() - current.touched <= IDLE_TTL_SECONDS:
                raise MetisError(ErrorCode.RATE_LIMITED, "dictation is busy", retry_after_seconds=1)
            self._current = Session(secrets.token_urlsafe(18), language, app, cfg, owner=owner)
            return self._snapshot(self._current)

    @staticmethod
    def _snapshot(s: Session) -> Session:
        return replace(s, pcm=bytearray(s.pcm))

    def _get_locked(self, session_id: str, owner: str) -> Session:
        s = self._current
        if (
            s is None
            or s.id != session_id
            or s.owner != owner
            or time.monotonic() - s.touched > IDLE_TTL_SECONDS
        ):
            raise MetisError(ErrorCode.NOT_FOUND, "dictation session not found or expired")
        return s

    def append(self, session_id: str, pcm: bytes, *, owner: str = "local") -> tuple[Session, bool]:
        with self._lock:
            s = self._get_locked(session_id, owner)
            if s.finishing:
                raise MetisError(ErrorCode.INVALID_STATE_TRANSITION, "dictation is finishing")
            if not pcm or len(pcm) % 2:
                raise MetisError(ErrorCode.VALIDATION_FAILED, "PCM16 chunks must have an even byte length")
            if len(s.pcm) + len(pcm) > self._max_bytes:
                raise MetisError(ErrorCode.LIMIT_EXCEEDED, "dictation is over the maximum recording length")
            s.pcm += pcm
            s.touched = time.monotonic()
            partial_due = s.partial_due()
            if partial_due:
                s.partial_at = len(s.pcm)
            return self._snapshot(s), partial_due

    def begin_finish(self, session_id: str, *, owner: str = "local") -> Session:
        with self._lock:
            s = self._get_locked(session_id, owner)
            if s.finishing:
                raise MetisError(ErrorCode.RATE_LIMITED, "dictation is finishing", retry_after_seconds=1)
            s.finishing = True
            s.touched = time.monotonic()
            return self._snapshot(s)

    def retry_finish(self, session_id: str, *, owner: str = "local") -> None:
        with self._lock:
            try:
                s = self._get_locked(session_id, owner)
            except MetisError:
                return
            s.finishing = False
            s.touched = time.monotonic()

    def end(self, session_id: str, *, owner: str = "local") -> None:
        with self._lock:
            self._get_locked(session_id, owner)
            self._current = None
