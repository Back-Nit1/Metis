"""Decode real files and return aggregate timing/accuracy without transcript text.

The caller must explicitly provide references to measure accuracy. In speed-only
mode neither existing ASR output nor fixture text is treated as ground truth.
"""

from __future__ import annotations

import hashlib
import platform
import statistics
import time
from pathlib import Path
from typing import Any

from metis_contracts import settings

from .evaluate import evaluate

AUDIO_SUFFIXES = frozenset({".webm", ".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".flac"})
METADATA_KEYS = frozenset(
    {
        "model_id",
        "artifact_revision",
        "compute_type",
        "cpu_threads",
        "beam_size",
        "language",
        "adapter_mode",
        "library_version",
        "temperature",
        "vad_enabled",
    }
)


def benchmark_recordings(
    directory: Path,
    transcriber: Any,
    *,
    references: list[dict[str, object]] | None = None,
    passes: int = 1,
) -> dict[str, Any]:
    if passes < 1:
        raise ValueError("passes must be positive")
    files: list[Path] = []
    hashes: set[bytes] = set()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES:
            digest = hashlib.sha256(path.read_bytes()).digest()
            if digest not in hashes:
                files.append(path)
                hashes.add(digest)
    if not files:
        raise ValueError("no supported recordings found")
    expected = {str(item["id"]) for item in references or []}
    if references is not None and expected != {path.stem for path in files}:
        raise ValueError("reference IDs must exactly match the unique recording filenames")
    hypotheses: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for pass_index in range(passes):
        for index, path in enumerate(files):
            audio = path.read_bytes()
            started = time.monotonic()
            text, _, metadata = transcriber.transcribe(audio, max_seconds=settings().limits.max_audio_seconds)
            wall = time.monotonic() - started
            hypotheses[path.stem] = text
            rows.append(
                {
                    "clip": index + 1,
                    "pass": pass_index + 1,
                    "wall_seconds": round(wall, 4),
                    "audio_seconds": metadata.get("audio_seconds"),
                    "decode_seconds": metadata.get("decode_seconds"),
                    "cold_start": metadata.get("cold_start"),
                    "model": {key: value for key, value in metadata.items() if key in METADATA_KEYS},
                }
            )
    durations = [row["audio_seconds"] for row in rows]
    audio_seconds = sum(float(duration) for duration in durations if duration is not None)
    wall_times = sorted(row["wall_seconds"] for row in rows)
    return {
        "measurement": "real_decoder",
        "machine": {"platform": platform.platform(), "python": platform.python_version()},
        "unique_recordings": len(files),
        "passes": passes,
        "wall_p50_seconds": statistics.median(wall_times),
        "wall_max_seconds": max(wall_times),
        "real_time_factor": round(sum(wall_times) / audio_seconds, 4) if audio_seconds else None,
        "rows": rows,
        "accuracy": evaluate(references, hypotheses) if references is not None else None,
        "accuracy_note": "Human reference transcripts supplied."
        if references is not None
        else "Not measured: no trusted human reference transcripts supplied.",
    }
