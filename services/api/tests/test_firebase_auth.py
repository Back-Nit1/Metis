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


def test_token_signed_by_another_key_is_rejected(monkeypatch, tokens):
    configure(monkeypatch)
    forged = tokens()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(auth, "_firebase_signing_key", lambda kid: other.public_key())
    with pytest.raises(MetisError):
        auth.verify_firebase_token(forged)
    reset_settings_cache()
