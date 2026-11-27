"""Revocable sessions for browsers on the hosted API's HTTPS origin."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from fastapi import Request
from metis_contracts import ErrorCode, MetisError, Mode, settings

from .auth import Principal, principal_from_header

COOKIE = "__Host-metis_browser"
BINDING_HEADER = "X-Metis-Session"
SESSION_SECONDS = 12 * 60 * 60
RECENT_SIGN_IN_SECONDS = 5 * 60
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


def enabled() -> bool:
    cfg = settings()
    return cfg.mode is Mode.SERVER and bool(cfg.firebase_project_id)


def _database() -> Path:
    # A separate file lets deployments invalidate sessions independently of the notebook.
    return Path(settings().sqlite_path).with_name("browser-sessions.db")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def binding(token: str) -> str:
    # A tab opened under another account must never silently adopt a replaced cookie.
    return _digest("tab:" + token)


def require_browser_origin(request: Request) -> None:
    origin = f"https://{request.url.netloc}"
    if request.url.scheme != "https" or request.headers.getlist("origin") != [origin]:
        raise MetisError(ErrorCode.FORBIDDEN, "browser sessions require a same-origin HTTPS request")


def start(principal: Principal, previous: str | None) -> str:
    now = int(time.time())
    if (
        principal.source != "firebase"
        or principal.auth_time is None
        or now - principal.auth_time > RECENT_SIGN_IN_SECONDS
    ):
        raise MetisError(ErrorCode.UNAUTHENTICATED, "sign in again to start a browser session")
    token = secrets.token_urlsafe(32)
    database = _database()
    os.close(os.open(database, os.O_CREAT | os.O_RDWR, 0o600))
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "token_hash TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id TEXT NOT NULL, "
            "expires_at INTEGER NOT NULL)"
        )
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        if previous and _TOKEN.fullmatch(previous):
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_digest(previous),))
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            (_digest(token), settings().firebase_project_id, principal.user_id, now + SESSION_SECONDS),
        )
    return token


def lookup(token: str | None) -> Principal:
    if token and _TOKEN.fullmatch(token) and _database().is_file():
        with closing(sqlite3.connect(_database())) as conn:
            row = conn.execute(
                "SELECT user_id FROM sessions WHERE token_hash = ? AND project_id = ? AND expires_at > ?",
                (_digest(token), settings().firebase_project_id, int(time.time())),
            ).fetchone()
        if row:
            return Principal(user_id=row[0], source="firebase")
    raise MetisError(ErrorCode.UNAUTHENTICATED, "sign in to continue")


def revoke(token: str | None) -> None:
    if token and _TOKEN.fullmatch(token) and _database().is_file():
        with closing(sqlite3.connect(_database())) as conn, conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_digest(token),))


def principal_for_request(request: Request, authorization: str | None) -> Principal:
    token = request.cookies.get(COOKIE)
    if authorization is not None or not token or not enabled():
        return principal_from_header(authorization)
    if not secrets.compare_digest(request.headers.get(BINDING_HEADER, "").encode(), binding(token).encode()):
        raise MetisError(ErrorCode.UNAUTHENTICATED, "browser session changed; sign in again")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        require_browser_origin(request)
    return lookup(token)
