"""Authentication: verified Firebase or Cognito JWTs, plus local identities.

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
    source: str  # "firebase", "cognito", "fixture" or "desktop"
    email: str | None = None
    auth_time: int | None = None

    @property
    def is_fixture(self) -> bool:
        return self.source == "fixture"


def _jwks_url() -> str:
    cfg = settings().aws
    return f"https://cognito-idp.{cfg.region}.amazonaws.com/{cfg.cognito_user_pool_id}/.well-known/jwks.json"


def _jwks() -> Any:
    """Fetch and cache the pool's JWKS. Keys rotate, so the cache has a TTL."""
    import jwt

    url = _jwks_url()
    cached = _JWKS_CACHE.get(url)
    now = time.time()
    if cached and now - cached[0] < _JWKS_TTL_SECONDS:
        return cached[1]
    client = jwt.PyJWKClient(url, cache_keys=True)
    _JWKS_CACHE[url] = (now, client)
    return client


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


_KNOWN_KIDS: set[str] = set()
_LAST_MISS = 0.0
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


def _refuse_unknown_kid_flood(token: str) -> None:
    """PyJWKClient refetches the JWKS over the network on every unknown ``kid``,
    so unauthenticated garbage tokens would become outbound requests. Allow one
    refresh per interval; everything else is refused locally."""
    import jwt

    global _LAST_MISS
    kid = jwt.get_unverified_header(token).get("kid")
    if kid is None or kid in _KNOWN_KIDS:
        return
    now = time.monotonic()
    if _KNOWN_KIDS and now - _LAST_MISS < _MISS_INTERVAL_SECONDS:
        raise MetisError(ErrorCode.UNAUTHENTICATED, "the token could not be verified")
    _LAST_MISS = now


def verify_cognito_token(token: str) -> Principal:
    """Validate signature, expiry, issuer and audience, then take the subject."""
    import jwt

    cfg = settings().aws
    if not cfg.cognito_user_pool_id or not cfg.cognito_client_id:
        raise MetisError(ErrorCode.UNAUTHENTICATED, "the identity provider is not configured")

    issuer = f"https://cognito-idp.{cfg.region}.amazonaws.com/{cfg.cognito_user_pool_id}"
    try:
        _refuse_unknown_kid_flood(token)
        # Cognito access tokens carry no ``aud`` (the app client is ``client_id``),
        # id tokens carry ``aud``. Signature, issuer and expiry are verified here;
        # the client binding is checked explicitly below, per token type.
        claims = jwt.decode(
            token,
            _jwks().get_signing_key_from_jwt(token).key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False, "require": ["exp", "iat", "sub", "token_use"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise MetisError(ErrorCode.UNAUTHENTICATED, "the access token has expired") from exc
    except Exception as exc:
        raise MetisError(ErrorCode.UNAUTHENTICATED, "the token could not be verified") from exc

    if kid := jwt.get_unverified_header(token).get("kid"):
        _KNOWN_KIDS.add(kid)
    token_use = claims.get("token_use")
    bound_client = claims.get("client_id") if token_use == "access" else claims.get("aud")
    if token_use not in ("access", "id") or bound_client != cfg.cognito_client_id:
        raise MetisError(ErrorCode.UNAUTHENTICATED, "the token was not issued to this client")
    subject = claims.get("sub")
    if not subject:
        raise MetisError(ErrorCode.UNAUTHENTICATED, "the token has no subject")
    return Principal(user_id=str(subject), source="cognito", email=claims.get("email"))


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
    return Principal(
        user_id=subject,
        source="firebase",
        email=email if isinstance(email, str) else None,
        auth_time=auth_time,
    )


def _desktop_principal(authorization: str | None) -> Principal:
    """Desktop-local mode: the bearer must be the launch secret, and is never
    sent to Cognito. The owner is the install subject, not the secret."""
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
        return verify_firebase_token(token) if firebase_project_id else verify_cognito_token(token)

    if firebase_project_id or cfg.environment == "prod" or not cfg.allow_fixture_identity:
        raise MetisError(ErrorCode.UNAUTHENTICATED, "authentication is required")
    return Principal(user_id=cfg.fixture_user_id, source="fixture")
