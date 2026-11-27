import pytest
from metis_contracts.config import Mode, Settings


@pytest.fixture
def server_env(monkeypatch):
    values = {
        "METIS_MODE": "server",
        "METIS_ENV": "prod",
        "METIS_ALLOW_FIXTURE_IDENTITY": "false",
        "METIS_FIREBASE_PROJECT_ID": "metis-test",
        "METIS_SPEECH_ENGINE": "faster_whisper",
        "METIS_INFERENCE_ENGINE": "local",
        "METIS_LOCAL_LLM_MODEL": "test-llm",
        "METIS_LOCAL_EMBED_MODEL": "test-embed",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in (
        "METIS_DESKTOP_AUTH_SECRET",
        "METIS_EMBED_ENGINE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_server_production_settings(server_env):
    cfg = Settings()
    assert cfg.mode is Mode.SERVER
    assert cfg.uses_real_inference
    assert cfg.firebase_project_id == "metis-test"


@pytest.mark.parametrize(
    "name,value",
    [
        ("METIS_ALLOW_FIXTURE_IDENTITY", "true"),
        ("METIS_DESKTOP_AUTH_SECRET", "s" * 32),
        ("METIS_FIREBASE_PROJECT_ID", ""),
        ("METIS_SPEECH_ENGINE", "fixture"),
        ("METIS_INFERENCE_ENGINE", "fixture"),
        ("METIS_INFERENCE_ENGINE", "auto"),
        ("METIS_LOCAL_LLM_MODEL", ""),
        ("METIS_LOCAL_EMBED_MODEL", ""),
        ("METIS_EMBED_ENGINE", "fixture"),
    ],
)
def test_server_fails_closed(server_env, monkeypatch, name, value):
    monkeypatch.setenv(name, name and value)
    with pytest.raises(ValueError):
        Settings()
