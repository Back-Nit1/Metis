"""SourceStore adapter: a server-owned local directory (local and server mode).

Two rules drive the whole design:

1. **The server picks the key.** A client never proposes an object path, and
   every read validates that the key sits under the owner's prefix. A key
   outside it is a permission error, not a miss.
2. **Stored bytes are the truth.** A submitted content-length or MIME header is
   advisory; ``head()`` reports what is actually stored and the completion path
   re-checks it against the configured limits.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

from metis_contracts import AdapterMode, ErrorCode, MetisError, ObjectRef, SystemClock, settings


def owner_prefix(user_id: str) -> str:
    """Deterministic, collision-free per-owner prefix.

    The subject is hashed so a raw authenticated identifier never appears in an
    object path (paths leak into logs and error messages).
    """
    digest = hashlib.sha256(user_id.encode()).hexdigest()[:32]
    return f"users/{digest}"


def build_key(user_id: str, capture_id: str, extension: str) -> str:
    return f"{owner_prefix(user_id)}/captures/{capture_id}{extension}"


_EXTENSIONS = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "application/json": ".json",
    "text/plain": ".txt",
}


def extension_for(content_type: str | None) -> str:
    base = (content_type or "").split(";")[0].strip().lower()
    return _EXTENSIONS.get(base, ".bin")


class LocalSourceStore:
    """Filesystem object store for local and server mode.

    The "upload URL" is an API route the browser PUTs to; there is no signing
    because nothing leaves the machine. The route still enforces the owner
    prefix and the byte limit.
    """

    mode = AdapterMode.LOCAL

    def __init__(self, root: str | None = None, clock: Any | None = None) -> None:
        self._root = Path(root or settings().local_objects_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._clock = clock or SystemClock()
        self.bucket = "local"

    def _path(self, ref: ObjectRef) -> Path:
        target = (self._root / ref.key).resolve()
        root = self._root.resolve()
        if not str(target).startswith(str(root) + "/"):
            # Defends against a traversal attempt in a key.
            raise MetisError(ErrorCode.FORBIDDEN, "object key escapes the store root")
        return target

    def upload_target(
        self, user_id: str, *, capture_id: str, content_type: str, max_bytes: int, ttl_seconds: int
    ) -> tuple[ObjectRef, dict[str, Any]]:
        key = build_key(user_id, capture_id, extension_for(content_type))
        ref = ObjectRef(bucket=self.bucket, key=key, content_type=content_type)
        instructions = {
            "url": f"/v1/local-uploads/{capture_id}",
            "method": "PUT",
            "headers": {"Content-Type": content_type},
            "fields": {},
            "object_key": key,
            "expires_at": (self._clock.now() + timedelta(seconds=ttl_seconds)).isoformat(),
            "max_bytes": max_bytes,
        }
        return ref, instructions

    def head(self, ref: ObjectRef) -> ObjectRef | None:
        path = self._path(ref)
        if not path.is_file():
            return None
        body = path.read_bytes()
        return ref.model_copy(
            update={"size_bytes": len(body), "checksum_sha256": hashlib.sha256(body).hexdigest()}
        )

    def get_bytes(self, ref: ObjectRef) -> bytes:
        path = self._path(ref)
        if not path.is_file():
            raise MetisError(ErrorCode.OBJECT_MISSING, f"object {ref.key} is not stored")
        return path.read_bytes()

    def put_bytes(self, ref: ObjectRef, body: bytes, *, content_type: str) -> ObjectRef:
        path = self._path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return ref.model_copy(
            update={
                "size_bytes": len(body),
                "checksum_sha256": hashlib.sha256(body).hexdigest(),
                "content_type": content_type,
            }
        )

    def delete(self, ref: ObjectRef) -> None:
        path = self._path(ref)
        if path.is_file():
            path.unlink()

    def delete_prefix(self, prefix: str) -> int:
        """Used by the purge job; returns the number of objects removed."""
        target = (self._root / prefix).resolve()
        root = self._root.resolve()
        if target == root or not str(target).startswith(str(root) + "/"):
            raise MetisError(ErrorCode.FORBIDDEN, "prefix escapes the store root")
        if not target.is_dir():
            return 0
        count = sum(1 for p in target.rglob("*") if p.is_file())
        shutil.rmtree(target)
        return count

    def owns_key(self, user_id: str, key: str) -> bool:
        # ".." would resolve outside the prefix on a filesystem.
        return key.startswith(owner_prefix(user_id) + "/") and ".." not in key.split("/")


def build_source_store(clock: Any | None = None) -> Any:
    return LocalSourceStore(clock=clock)
