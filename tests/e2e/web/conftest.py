"""Black-box browser journeys for the Metis web app.

Target: `METIS_BASE_URL` (+ `METIS_API_URL` for direct API seeding). When unset
the fixture below boots an isolated stack (fixture adapters, fresh SQLite, local
worker, Vite) on private ports, so the suite needs no cloud account and never
touches the deployed app. A deployed target is READ-ONLY: mutating journeys skip.

Everything that passes against the local stack is evidence about FIXTURE
adapters only (fixture inference / transcriber); it says nothing about real
models or real ASR.

Opt-in: these tests need a browser and servers, so they are marked `webqa` and
`integration` (excluded from `make test`). Run with `pytest tests/e2e/web -m webqa`.
CI-safe subset: `-m "webqa and ci_safe"` (1280x800 light only, no deployed target).
Full matrix: `METIS_QA_MATRIX=1` (3 viewports x light/dark).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")
import contextlib

from axe_playwright_python.sync_playwright import Axe

ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = ROOT / "ops" / "qa-artifacts"
RESULTS = ARTIFACTS / "results.jsonl"
# "<owner pid> <pgid>" per server group this harness started; lets the next run
# reap groups a crashed or killed run (no teardown) left behind.
PIDFILE = Path(tempfile.gettempdir()) / "metis-qa-servers.pids"

VIEWPORTS = {
    "desktop": {"viewport": {"width": 1280, "height": 800}},
    "tablet": {
        "viewport": {"width": 768, "height": 1024},
        "is_mobile": True,
        "has_touch": True,
        "device_scale_factor": 1,
    },
    "phone": {
        "viewport": {"width": 390, "height": 844},
        "is_mobile": True,
        "has_touch": True,
        "device_scale_factor": 1,
    },
}
FULL = os.environ.get("METIS_QA_MATRIX") == "1"
PROFILES = [(v, s) for v in VIEWPORTS for s in ("light", "dark")] if FULL else [("desktop", "light")]
DEPLOYED = bool(os.environ.get("METIS_BASE_URL"))


def pytest_configure(config):
    for line in (
        "webqa: browser journey against a running Metis web app",
        "ci_safe: local fixture stack only, no network to the deployed app",
        "known_issue(owner, note): confirmed defect, expected to fail until fixed",
        "mutating: writes data; skipped against METIS_BASE_URL",
    ):
        config.addinivalue_line("markers", line)


def pytest_collection_modifyitems(config, items):
    here = str(Path(__file__).parent)
    for item in items:
        if not str(item.fspath).startswith(here):
            continue
        item.add_marker(pytest.mark.webqa)
        item.add_marker(pytest.mark.integration)
        if item.get_closest_marker("known_issue"):
            item.add_marker(pytest.mark.xfail(reason="@known-issue", strict=False))
        if DEPLOYED and item.get_closest_marker("mutating"):
            item.add_marker(pytest.mark.skip(reason="mutating journey: local stack only"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, timeout: float = 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f"{url} did not come up")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_group(pgid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig)
        for _ in range(20):
            if not _alive(pgid):
                return
            time.sleep(0.1)


def _reap_stale_servers() -> None:
    """Kill server groups from earlier runs whose pytest owner is gone."""
    if not PIDFILE.exists():
        return
    keep = []
    for line in PIDFILE.read_text().splitlines():
        owner, pgid = map(int, line.split())
        if _alive(owner):
            keep.append(line)  # a concurrent run still owns it
            continue
        # Any member still running our code (the vite child can outlive pnpm);
        # never signal a recycled group that is not ours.
        if subprocess.run(["pgrep", "-g", str(pgid), "-f", "metis"], capture_output=True).returncode == 0:
            _kill_group(pgid)
    PIDFILE.write_text("".join(f"{line}\n" for line in keep))


class Stack:
    def __init__(self, web: str, api: str):
        self.web, self.api = web, api


@pytest.fixture(scope="session")
def stack():
    if DEPLOYED:
        yield Stack(os.environ["METIS_BASE_URL"].rstrip("/"), os.environ.get("METIS_API_URL", "").rstrip("/"))
        return
    if os.environ.get("METIS_QA_USE_RUNNING"):  # reuse `make dev` on 5173/8000
        yield Stack("http://localhost:5173", "http://localhost:8000")
        return
    _reap_stale_servers()
    tmp = tempfile.mkdtemp(prefix="metis-qa-")
    api_port, web_port = _free_port(), _free_port()
    env = {
        **os.environ,
        "METIS_MODE": "local",
        "METIS_SQLITE_PATH": f"{tmp}/metis.db",
        "METIS_LOCAL_OBJECTS_DIR": f"{tmp}/objects",
        "METIS_RATE_PER_MINUTE": "100000",
        "METIS_RATE_PER_HOUR": "1000000",
        "METIS_RATE_GLOBAL_PER_MINUTE": "100000",
        "METIS_CONNECT_RATE_PER_MINUTE": "100000",
        "METIS_CORS_ORIGINS": f"http://localhost:{web_port}",
    }
    env.pop("VIRTUAL_ENV", None)
    api_url = f"http://localhost:{api_port}"
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "metis_api.main:app", "--port", str(api_port)],
            env=env,
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ),
        subprocess.Popen(
            [sys.executable, "-m", "metis_pipeline.local_worker", "--interval", "0.5"],
            env=env,
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ),
        subprocess.Popen(
            ["pnpm", "--filter", "@metis/web", "exec", "vite", "--port", str(web_port), "--strictPort"],
            env={**env, "VITE_METIS_API": api_url},
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ),
    ]
    with PIDFILE.open("a") as f:
        f.writelines(f"{os.getpid()} {p.pid}\n" for p in procs)
    try:
        _wait(f"{api_url}/v1/health")
        _wait(f"http://localhost:{web_port}/")
        yield Stack(f"http://localhost:{web_port}", api_url)
    finally:
        # terminate() alone only reached the pnpm wrapper and orphaned vite.
        for p in procs:
            _kill_group(p.pid)


@pytest.fixture(params=PROFILES, ids=lambda p: f"{p[0]}-{p[1]}", autouse=True)
def profile(request):
    return request.param


@pytest.fixture
def browser_context_args(browser_context_args, profile):
    viewport, scheme = profile
    return {**browser_context_args, **VIEWPORTS[viewport], "color_scheme": scheme}


@pytest.fixture
def api(playwright, stack):
    """Direct API client for seeding/verifying; same fixture identity the browser uses."""
    ctx = playwright.request.new_context(base_url=stack.api or None)
    yield ctx
    ctx.dispose()


@pytest.fixture
def probe(page, request, profile):
    """Collects console errors, failed calls, overflow and axe findings per journey."""
    seen = {"console": [], "failed": [], "http": []}
    page.on("console", lambda m: seen["console"].append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: seen["console"].append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: seen["failed"].append(f"{r.method} {r.url} {r.failure}"))
    page.on(
        "response",
        lambda r: seen["http"].append(f"{r.status} {r.request.method} {r.url}") if r.status >= 400 else None,
    )
    yield seen
    overflow = False
    axe = []
    try:
        overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
        axe = [
            {"id": v["id"], "impact": v["impact"], "nodes": len(v["nodes"])}
            for v in Axe().run(page).response["violations"]
        ]
    except Exception:  # page closed / navigated away mid-teardown
        pass
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "test": request.node.name,
                    "profile": "-".join(profile),
                    **seen,
                    "overflow": overflow,
                    "axe": axe,
                }
            )
            + "\n"
        )
    assert not overflow, "horizontal overflow"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    page = item.funcargs.get("page") if rep.when == "call" and rep.failed else None
    if page:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        name = item.name.replace("[", "_").replace("]", "")
        with contextlib.suppress(Exception):
            page.screenshot(path=str(ARTIFACTS / f"fail-{name}.png"))
