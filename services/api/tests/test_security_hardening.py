"""Regression tests for findings from the open-source security review.

Each test pins one attack that worked before the fix.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metis_contracts import MetisError, reset_settings_cache

NY = "America/New_York"
T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)

AUDIO = {
    "kind": "audio",
    "content_type": "audio/webm",
    "expected_bytes": 10,
    "capture_timezone": NY,
    "captured_at": T0.isoformat(),
}


def _audio_capture(client) -> dict:
    return client.post("/v1/captures", json=AUDIO, headers={"Idempotency-Key": "audio"}).json()


# --- request edge ------------------------------------------------------------ #


def test_dns_rebound_host_header_is_refused(client):
    assert client.get("/v1/captures", headers={"Host": "evil.example.com"}).status_code == 403
    assert client.get("/v1/captures", headers={"Host": "localhost:8000"}).status_code == 200
    assert client.get("/v1/captures", headers={"Host": "[::1]:8000"}).status_code == 200


def test_oversized_json_body_is_refused_while_streaming(client):
    response = client.post("/v1/tasks", content=b'{"title":"' + b"a" * (3 * 1024 * 1024) + b'"}')
    assert response.status_code == 413


def test_oversized_upload_is_refused_before_it_is_buffered(client, monkeypatch):
    monkeypatch.setenv("METIS_MAX_UPLOAD_BYTES", "1000")
    reset_settings_cache()
    capture = _audio_capture(client)
    response = client.put(
        capture["upload"]["url"], content=b"\0" * 5000, headers={"Content-Type": "audio/webm"}
    )
    assert response.status_code == 413


def test_correlation_id_is_a_short_safe_token(client):
    hostile = "A" * 5000 + "\x1b[31m"
    echoed = client.get("/v1/captures", headers={"X-Correlation-Id": "x" * 65}).headers["x-correlation-id"]
    assert echoed.startswith("run_")
    assert hostile not in echoed
    assert (
        client.get("/v1/captures", headers={"X-Correlation-Id": "trace-1.ok"}).headers["x-correlation-id"]
        == "trace-1.ok"
    )


def test_idempotency_key_is_bounded(client):
    response = client.post("/v1/tasks", json={"title": "t"}, headers={"Idempotency-Key": "k" * 129})
    assert response.status_code == 422


# --- uploads ----------------------------------------------------------------- #


def test_upload_must_match_the_declared_media_type(client):
    capture = _audio_capture(client)
    response = client.put(capture["upload"]["url"], content=b"x", headers={"Content-Type": "text/html"})
    assert response.status_code == 415


def test_a_completed_recording_cannot_be_overwritten(client):
    capture = _audio_capture(client)
    url = capture["upload"]["url"]
    headers = {"Content-Type": "audio/webm"}
    assert client.put(url, content=b"original", headers=headers).status_code == 204
    done = client.post(
        f"/v1/captures/{capture['capture_id']}/complete",
        json={"expected_revision": capture["revision"], "actual_bytes": 8},
        headers={"Idempotency-Key": "complete-1"},
    )
    assert done.status_code == 200, done.text
    assert client.put(url, content=b"swapped!", headers=headers).status_code == 422


# --- identity ---------------------------------------------------------------- #


def test_fixture_identity_cannot_be_enabled_in_server_mode_even_in_staging(monkeypatch):
    monkeypatch.setenv("METIS_MODE", "server")
    monkeypatch.setenv("METIS_ENV", "staging")
    monkeypatch.setenv("METIS_ALLOW_FIXTURE_IDENTITY", "true")
    reset_settings_cache()
    from metis_contracts import settings

    with pytest.raises(ValueError, match="fixture"):
        settings()
    reset_settings_cache()


# --- referential integrity --------------------------------------------------- #


def test_task_cannot_point_at_a_missing_project_or_source(client):
    assert client.post("/v1/tasks", json={"title": "t", "project_id": "proj_nope"}).status_code == 422
    ref = {"source_id": "src_nope", "source_version": 1, "span_ref": None}
    assert client.post("/v1/tasks", json={"title": "t", "source_refs": [ref]}).status_code == 422
    assert client.post("/v1/tasks", json={"title": "t"}).status_code == 201


def test_project_ids_are_plain_tokens(client):
    stamp = "2026-10-05T14:00:00Z"
    base = {"user_id": "u", "created_at": stamp, "updated_at": stamp, "title": "p"}
    for index, bad in enumerate(("proj_a#b", "proj_../x", "proj_%00")):
        assert (
            client.post(
                "/v1/projects",
                json={**base, "id": bad},
                headers={"Idempotency-Key": f"bad-project-{index}"},
            ).status_code
            == 422
        )
    assert (
        client.post(
            "/v1/projects",
            json={**base, "id": "proj_ok_1"},
            headers={"Idempotency-Key": "project-ok"},
        ).status_code
        == 201
    )


def test_search_has_its_own_ceiling(client, monkeypatch):
    import metis_api.deps as deps

    monkeypatch.setattr(deps, "_limiter", deps.RateLimiter())
    monkeypatch.setenv("METIS_RATE_PER_MINUTE", "60")  # search gets a sixth of it
    reset_settings_cache()
    codes = [client.post("/v1/search", json={"query": "q"}).status_code for _ in range(12)]
    assert codes[:10] == [200] * 10
    assert codes[10] == 429


# --- fixture identity is loopback-only; sliding-window limits ----------------- #


def _client_from(adapters, peer: str):
    from fastapi.testclient import TestClient
    from metis_api.main import create_app

    app = create_app()
    client = TestClient(app, client=(peer, 50000))
    client.__enter__()
    app.state.adapters = adapters
    return client


def test_fixture_identity_is_refused_for_a_lan_caller(adapters):
    # A LAN caller can forge Host: localhost, so the Host guard alone is not enough.
    lan = _client_from(adapters, "192.168.1.20")
    try:
        assert lan.get("/v1/captures", headers={"Host": "localhost"}).status_code == 401
    finally:
        lan.__exit__(None, None, None)
    local = _client_from(adapters, "127.0.0.1")
    try:
        assert local.get("/v1/captures").status_code == 200
    finally:
        local.__exit__(None, None, None)


def test_rate_limiter_is_a_true_sliding_window(monkeypatch):
    import metis_api.deps as deps
    import metis_api.rate_limit as rate_limit

    now = [120.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: now[0])
    limiter = deps.RateLimiter()
    limiter.check("k", limit=3)
    now[0] = 121.0
    limiter.check("k", limit=3)
    now[0] = 179.0
    limiter.check("k", limit=3)
    with pytest.raises(MetisError) as refused:
        limiter.check("k", limit=3)
    assert refused.value.retry_after_seconds == 1
    now[0] = 180.0
    # Only the request at 120 expires; this is not a full boundary reset.
    assert limiter.check("k", limit=3).remaining == 0
    with pytest.raises(MetisError):
        limiter.check("k", limit=3)


def test_every_rule_must_pass_and_a_refusal_counts_nothing(monkeypatch):
    import metis_api.deps as deps
    import metis_api.rate_limit as rate_limit

    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: 0.0)
    limiter = deps.RateLimiter()
    rules = [("user:a", 10, 60), ("user:a", 2, 3600), ("global", 100, 60)]
    limiter.check_all(rules)
    limiter.check_all(rules)
    with pytest.raises(MetisError):
        limiter.check_all(rules)  # hourly rule refuses
    # The refused call did not burn a minute slot or a global slot.
    assert len(limiter._events[("user:a", 60)]) == 2
    assert len(limiter._events[("global", 60)]) == 2


def test_connection_limit_is_100_per_rolling_minute_and_health_is_exempt(adapters, monkeypatch):
    import metis_api.rate_limit as rate_limit

    now = [100.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: now[0])
    client = _client_from(adapters, "203.0.113.7")
    try:
        for index in range(100):
            response = client.get("/")
            assert response.status_code == 200
            assert response.headers["ratelimit-limit"] == "100"
            assert response.headers["ratelimit-remaining"] == str(99 - index)
        refused = client.get("/")
        assert refused.status_code == 429
        assert refused.headers["retry-after"] == "60"
        assert refused.headers["ratelimit-remaining"] == "0"
        assert refused.json()["code"] == "rate_limited"
        # Monitoring remains able to observe the service while a client is limited.
        assert client.get("/v1/health").status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_forwarded_client_is_only_used_from_a_trusted_proxy():
    import ipaddress

    from metis_api.rate_limit import _client_identity

    trusted = (ipaddress.ip_network("127.0.0.0/8"),)
    header = [(b"x-forwarded-for", b"198.51.100.9, 203.0.113.8")]
    direct = {"client": ("192.0.2.1", 443), "headers": header}
    proxied = {"client": ("127.0.0.1", 443), "headers": header}
    assert _client_identity(direct, trusted) == "192.0.2.1"
    # The nearest untrusted hop wins, so a forged leftmost value cannot evade limits.
    assert _client_identity(proxied, trusted) == "203.0.113.8"


def test_global_ceiling_limits_all_users_together(client, monkeypatch):
    import metis_api.deps as deps

    monkeypatch.setattr(deps, "_limiter", deps.RateLimiter())
    monkeypatch.setenv("METIS_RATE_GLOBAL_PER_MINUTE", "3")
    reset_settings_cache()
    codes = [client.get("/v1/captures").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
