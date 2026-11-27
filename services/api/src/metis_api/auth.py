"""Authentication: verified Firebase / Identity Platform JWTs, plus local identities.

The ownership rule the whole system depends on lives here: ``user_id`` comes
from the verified token subject and from nowhere else. A request body, a query
parameter or a queue payload never decides who owns a record.

Production refuses fixture identity. ``Settings`` already rejects
``METIS_ENV=prod`` with fixture identity enabled, and this module double-checks
at request time so a misconfiguration cannot silently authenticate everyone as
the demo user.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from metis_contracts import ErrorCode, MetisError, settings

log = logging.getLogger("metis.api.auth")

_JWKS_CACHE: dict[str, tuple[float, Any]] = {}
_JWKS_TTL_SECONDS = 3600
_FIREBASE_JWKS_URL = (
    "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"
)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. ``user_id`` is the partition key for all data."""

    user_id: str
    source: str  # "firebase", "fixture" or "desktop"
    email: str | None = None

    @property
    def is_fixture(self) -> bool:
        return self.source == "fixture"


def _firebase_jwks() -> Any:
    """Google's rotating secure-token keys, cached as a set rather than forever per key."""
    import jwt

    cached = _JWKS_CACHE.get(_FIREBASE_JWKS_URL)
    now = time.time()
    if cached and now - cached[0] < _JWKS_TTL_SECONDS:
        return cached[1]
    client = jwt.PyJWKClient(_FIREBASE_JWKS_URL, cache_keys=False, lifespan=_JWKS_TTL_SECONDS, timeout=5)
    _JWKS_CACHE[_FIREBASE_JWKS_URL] = (now, client)
    return client


_MISS_INTERVAL_SECONDS = 30
_FIREBASE_REFRESH_LOCK = threading.Lock()
_FIREBASE_LAST_REFRESH = 0.0


def _firebase_signing_key(kid: str) -> Any:
    """Use cached keys first; rate-limit refreshes triggered by unknown key ids."""
    global _FIREBASE_LAST_REFRESH
    client = _firebase_jwks()
    keys = client.get_signing_keys()
    match = next((key for key in keys if key.key_id == kid), None)
    if match is not None:
        return match.key
    with _FIREBASE_REFRESH_LOCK:
        now = time.monotonic()
        if _FIREBASE_LAST_REFRESH and now - _FIREBASE_LAST_REFRESH < _MISS_INTERVAL_SECONDS:
            raise ValueError("unknown signing key")
        _FIREBASE_LAST_REFRESH = now
        keys = client.get_signing_keys(refresh=True)
    match = next((key for key in keys if key.key_id == kid), None)
    if match is None:
        raise ValueError("unknown signing key")
    return match.key


def verify_firebase_token(token: str) -> Principal:
    """Verify a Firebase ID token for the configured project before using its uid.

    This checks the signature and token claims, but does not query Firebase for
    revocation or account disablement; ID tokens remain valid until expiry.
    """
    import jwt

    project_id = getattr(settings(), "firebase_project_id", None)
    if not project_id:
        raise MetisError(ErrorCode.UNAUTHENTICATED, "the identity provider is not configured")
    try:
        header = jwt.get_unverified_header(token)
        if not isinstance(header.get("kid"), str) or not header["kid"]:
            raise ValueError("missing signing key id")
        claims = jwt.decode(
            token,
            _firebase_signing_key(header["kid"]),
            algorithms=["RS256"],
            issuer=f"https://securetoken.google.com/{project_id}",
            audience=project_id,
            options={
                "require": ["exp", "iat", "auth_time", "sub", "aud", "iss"],
                "strict_aud": True,
            },
        )
        subject = claims["sub"]
        auth_time = claims["auth_time"]
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 128:
            raise ValueError("invalid subject")
        if isinstance(auth_time, bool) or not isinstance(auth_time, int) or auth_time > time.time():
            raise ValueError("invalid authentication time")
    except jwt.ExpiredSignatureError as exc:
        raise MetisError(ErrorCode.UNAUTHENTICATED, "the access token has expired") from exc
    except Exception as exc:
        raise MetisError(ErrorCode.UNAUTHENTICATED, "the token could not be verified") from exc

    email = claims.get("email")
    return Principal(user_id=subject, source="firebase", email=email if isinstance(email, str) else None)


def _desktop_principal(authorization: str | None) -> Principal:
    """Desktop-local mode: the bearer must be the launch secret, and is never
    sent to an identity provider. The owner is the install subject, not the secret."""
    from .desktop import install_subject

    cfg = settings()
    if cfg.environment == "prod":
        raise MetisError(ErrorCode.UNAUTHENTICATED, "desktop authentication is not available here")
    scheme, _, value = (authorization or "").partition(" ")
    secret = cfg.desktop.auth_secret or ""
    # compare_digest on bytes: str comparison rejects non-ASCII input.
    if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip().encode(), secret.encode()):
        raise MetisError(ErrorCode.UNAUTHENTICATED, "authentication is required")
    return Principal(user_id=install_subject(cfg.desktop.identity_path), source="desktop")


def principal_from_header(authorization: str | None) -> Principal:
    """Resolve the caller from an Authorization header.

    In a non-production configuration with fixture identity enabled, a missing
    header resolves to the demo subject so the local app needs no login. That
    path is refused outright for ``METIS_ENV=prod``.
    """
    cfg = settings()
    firebase_project_id = getattr(cfg, "firebase_project_id", None)
    if cfg.desktop.enabled and not firebase_project_id:
        return _desktop_principal(authorization)
    token = None
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise MetisError(ErrorCode.UNAUTHENTICATED, "expected an 'Authorization: Bearer <token>' header")
        token = value.strip()

    if token:
        return verify_firebase_token(token)

    if firebase_project_id or cfg.environment == "prod" or not cfg.allow_fixture_identity:
        raise MetisError(ErrorCode.UNAUTHENTICATED, "authentication is required")
    return Principal(user_id=cfg.fixture_user_id, source="fixture")
