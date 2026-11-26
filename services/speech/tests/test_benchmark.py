"""Real-recording benchmark must never report reference echo as measurement."""

from pathlib import Path

import pytest
from metis_speech.benchmark import benchmark_recordings
from metis_speech.evaluate import main


class Decoder:
    mode = "local"

    def transcribe(self, audio, **kwargs):
        return "Call Cerno tomorrow", [], {"audio_seconds": 5.0, "decode_seconds": 0.1, "model_id": "test"}


def test_speed_only_deduplicates_audio_and_never_exposes_text(tmp_path):
    (tmp_path / "first.webm").write_bytes(b"audio")
    (tmp_path / "duplicate.webm").write_bytes(b"audio")
    report = benchmark_recordings(tmp_path, Decoder(), passes=2)
    assert report["unique_recordings"] == 1
    assert len(report["rows"]) == 2
    assert report["accuracy"] is None
    assert "Call Cerno" not in str(report)


def test_accuracy_requires_exact_reference_coverage(tmp_path):
    (tmp_path / "real.webm").write_bytes(b"audio")
    with pytest.raises(ValueError, match="exactly match"):
        benchmark_recordings(tmp_path, Decoder(), references=[{"id": "different", "reference": "echo"}])
    report = benchmark_recordings(
        tmp_path, Decoder(), references=[{"id": "real", "reference": "Call Cerno tomorrow"}]
    )
    assert report["accuracy"]["splits"]["dev"]["mean_wer"] == 0
    assert "Call Cerno" not in str(report)


def test_real_recording_cli_rejects_fixture_decoder(tmp_path, monkeypatch):
    from metis_speech.transcriber import FixtureTranscriber

    monkeypatch.setattr("metis_speech.transcriber.build_transcriber", FixtureTranscriber)
    with pytest.raises(SystemExit) as exc:
        main(["--audio-dir", str(tmp_path), "--speed-only"])
    assert exc.value.code == 2


def test_empty_audio_dir_is_not_a_successful_benchmark(tmp_path):
    with pytest.raises(ValueError, match="no supported recordings"):
        benchmark_recordings(Path(tmp_path), Decoder())


def test_remote_fixture_output_is_never_labeled_real_decoder(tmp_path):
    class FixtureEndpoint:
        def transcribe(self, audio, **kwargs):
            return "fixture echo", [], {"adapter_mode": "fixture"}

    (tmp_path / "clip.webm").write_bytes(b"audio")
    with pytest.raises(ValueError, match="fixture output"):
        benchmark_recordings(tmp_path, FixtureEndpoint())
