import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "hosted_env", Path(__file__).parents[1] / "render-hosted-env.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def config():
    return {
        "METIS_FIREBASE_PROJECT_ID": "metis-test",
        "METIS_LOCAL_LLM_MODEL": "example:1",
        "METIS_LOCAL_EMBED_MODEL": "embed:2",
    }


def test_hosted_config_clears_shared_identity():
    result = module.render(config())
    assert "METIS_DESKTOP_AUTH_SECRET=\n" in result
    assert "METIS_MODE=server\n" in result
    assert "METIS_ENV=prod\n" in result
    assert "METIS_ALLOW_FIXTURE_IDENTITY=false\n" in result
    assert "https://tauri.localhost" in result


@pytest.mark.parametrize(
    "key,value",
    [
        ("METIS_FIREBASE_PROJECT_ID", ""),
        ("METIS_LOCAL_LLM_MODEL", "model\nMETIS_ALLOW_FIXTURE_IDENTITY=true"),
        ("METIS_LOCAL_EMBED_MODEL", ""),
        ("METIS_WEB_ORIGIN", "https://user:password@example.test"),
        ("METIS_WEB_ORIGIN", "http://example.test"),
        ("METIS_WEB_ORIGIN", "https://example.test/path"),
    ],
)
def test_bad_hosted_config_refused(key, value):
    env = config()
    env[key] = value
    with pytest.raises(ValueError):
        module.render(env)
