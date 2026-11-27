#!/usr/bin/env python3
"""Render a non-secret server configuration, failing before any VM changes."""

import os
import re
import sys


def render(env: dict[str, str]) -> str:
    project = env.get("METIS_FIREBASE_PROJECT_ID", "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
        raise ValueError("set METIS_FIREBASE_PROJECT_ID to the Identity Platform project ID")
    values = {
        "METIS_MODE": "server",
        "METIS_ENV": "prod",
        "METIS_ALLOW_FIXTURE_IDENTITY": "false",
        # Overrides the old single-user EnvironmentFile; never delete its key/data.
        "METIS_DESKTOP_AUTH_SECRET": "",
        "METIS_FIREBASE_PROJECT_ID": project,
        "METIS_INFERENCE_ENGINE": "local",
        "METIS_EMBED_ENGINE": "local",
        "METIS_LOCAL_LLM_ENDPOINT": "http://127.0.0.1:11434",
    }
    for key in ("METIS_LOCAL_LLM_MODEL", "METIS_LOCAL_EMBED_MODEL", "METIS_FLOW_MODEL"):
        default = values["METIS_LOCAL_LLM_MODEL"] if key == "METIS_FLOW_MODEL" else ""
        value = env.get(key, default)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value):
            raise ValueError(f"set {key} to an already installed server model")
        values[key] = value
    values["METIS_FLOW_MODEL_ENDPOINT"] = values["METIS_LOCAL_LLM_ENDPOINT"]
    # Desktop webview origins only; no browser app is hosted.
    values["METIS_CORS_ORIGINS"] = "tauri://localhost,http://tauri.localhost,https://tauri.localhost"
    return "".join(f"{key}={value}\n" for key, value in values.items())


if __name__ == "__main__":
    try:
        sys.stdout.write(render(dict(os.environ)))
    except ValueError as error:
        raise SystemExit(str(error)) from error
