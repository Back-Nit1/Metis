"""Real signed tokens and separate server-owned capture partitions."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from metis_api import auth
from metis_contracts import MetisError, reset_settings_cache


@pytest.fixture
def tokens(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(auth, "_firebase_signing_key", lambda kid: key.public_key())

    def make(subject="alice", **updates):
        now = int(time.time())
        claims = {
            "sub": subject,
            "aud": "metis-test",
            "iss": "https://securetoken.google.com/metis-test",
            "iat": now,
            "exp": now + 3600,
            "auth_time": now,
        }
        claims.update(updates)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})

    return make


def configure(monkeypatch):
    monkeypatch.setenv("METIS_FIREBASE_PROJECT_ID", "metis-test")
    reset_settings_cache()


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "wrong-project"},
        {"iss": "https://attacker.test"},
        {"exp": 1},
        {"sub": ""},
        {"auth_time": 9999999999},
        {"iat": 9999999999},
    ],
)
def test_invalid_claims_rejected(monkeypatch, tokens, claims):
    configure(monkeypatch)
    with pytest.raises(MetisError):
        auth.verify_firebase_token(tokens(**claims))
    reset_settings_cache()


def test_firebase_does_not_fall_back_to_shared_identity(monkeypatch, tokens):
    configure(monkeypatch)
    monkeypatch.setenv("METIS_DESKTOP_AUTH_SECRET", "s" * 32)
    reset_settings_cache()
    for header in (None, "Bearer " + "s" * 32):
        with pytest.raises(MetisError):
            auth.principal_from_header(header)
    assert auth.principal_from_header("Bearer " + tokens()).user_id == "alice"
    reset_settings_cache()


def test_captures_are_saved_on_server_and_isolated_by_verified_user(client, adapters, monkeypatch, tokens):
    configure(monkeypatch)
    alice = {"Authorization": "Bearer " + tokens(), "Idempotency-Key": "same-client-key"}
    bob = {"Authorization": "Bearer " + tokens("bob"), "Idempotency-Key": "same-client-key"}
    body = {
        "kind": "text",
        "text": "Call Sam tomorrow",
        "capture_timezone": "UTC",
        "captured_at": "2026-11-26T12:00:00Z",
        "client_capture_id": "same-device-id",
    }
    result = client.post("/v1/captures", headers=alice, json=body)
    assert result.status_code == 201, result.text
    source_id = result.json()["capture_id"]
    assert client.get(f"/v1/captures/{source_id}", headers=alice).json()["transcript_text"] == body["text"]
    assert client.get(f"/v1/captures/{source_id}", headers=bob).status_code == 404
    assert (
        client.patch(
            f"/v1/captures/{source_id}/transcript",
            headers=bob,
            json={"expected_revision": 1, "text": "stolen"},
        ).status_code
        == 404
    )
    second = client.post("/v1/captures", headers=bob, json=body)
    assert second.status_code == 201
    assert second.json()["capture_id"] != source_id
    assert client.post("/v1/captures", headers=alice, json=body).json()["capture_id"] == source_id
    assert adapters.state_store.get("alice", "source", source_id).transcripts[0].text == body["text"]


def test_browser_session_is_bound_to_the_tab_and_revoked_on_sign_out(client, monkeypatch, tokens):
    for key, value in {
        "METIS_MODE": "server",
        "METIS_ENV": "prod",
        "METIS_ALLOW_FIXTURE_IDENTITY": "false",
        "METIS_INFERENCE_ENGINE": "local",
        "METIS_LOCAL_LLM_MODEL": "chat-test",
        "METIS_LOCAL_EMBED_MODEL": "embed-test",
        "METIS_SPEECH_ENGINE": "faster_whisper",
    }.items():
        monkeypatch.setenv(key, value)
    configure(monkeypatch)
    from metis_api.browser_session import BINDING_HEADER, COOKIE

    origin = "https://testserver"
    session_url = origin + "/v1/session"
    preferences = origin + "/v1/preferences"

    def sign_in(subject="alice", **claims):
        return client.post(
            session_url,
            headers={"Origin": origin, "Authorization": "Bearer " + tokens(subject, **claims)},
        )

    def rotate():
        response = sign_in("bob")
        assert response.status_code == 204
        return response.headers[BINDING_HEADER]

    assert client.get(session_url).status_code == 401
    assert client.post(session_url, headers={"Authorization": "Bearer " + tokens()}).status_code == 403
    assert sign_in(auth_time=int(time.time()) - 901).status_code == 401
    created = sign_in()
    assert created.status_code == 204
    cookie = created.headers["set-cookie"]
    assert all(option in cookie for option in (COOKIE, "HttpOnly", "Secure", "Path=/", "SameSite=lax"))
    assert "Domain=" not in cookie
    alice_binding = created.headers[BINDING_HEADER]
    alice_token = client.cookies.get(COOKIE)
    assert client.get(session_url).headers[BINDING_HEADER] == alice_binding
    assert client.get(preferences).status_code == 401
    assert client.get(preferences, headers={BINDING_HEADER: alice_binding}).status_code == 200
    capture = client.post(
        origin + "/v1/captures",
        headers={"Origin": origin, BINDING_HEADER: alice_binding, "Idempotency-Key": "browser-capture"},
        json={
            "kind": "text",
            "text": "Alice's note",
            "capture_timezone": "UTC",
            "captured_at": "2026-11-26T12:00:00Z",
        },
    )
    assert capture.status_code == 201
    assert (
        client.delete(
            session_url, headers={"Origin": "https://other.test", BINDING_HEADER: alice_binding}
        ).status_code
        == 403
    )
    bob_binding = rotate()
    assert bob_binding != alice_binding
    assert client.get(preferences, headers={BINDING_HEADER: alice_binding}).status_code == 401
    assert client.get(preferences, headers={BINDING_HEADER: bob_binding}).status_code == 200
    assert (
        client.get(
            origin + "/v1/captures/" + capture.json()["capture_id"],
            headers={BINDING_HEADER: bob_binding},
        ).status_code
        == 404
    )
    assert (
        client.get(
            preferences, headers={"Cookie": f"{COOKIE}={alice_token}", BINDING_HEADER: alice_binding}
        ).status_code
        == 401
    )
    assert client.patch(preferences, json={}, headers={BINDING_HEADER: bob_binding}).status_code == 403
    bob_token = client.cookies.get(COOKIE)
    assert (
        client.delete(session_url, headers={"Origin": origin, BINDING_HEADER: bob_binding}).status_code == 204
    )
    assert (
        client.get(
            preferences, headers={"Cookie": f"{COOKIE}={bob_token}", BINDING_HEADER: bob_binding}
        ).status_code
        == 401
    )
    assert client.get(preferences, headers={"Authorization": "Bearer " + tokens()}).status_code == 200
