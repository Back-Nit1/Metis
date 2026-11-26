"""Authenticated proxy to the private, single-process dictation service.

The client never receives the speech secret or chooses the speech-side owner.
All API workers use the same speech process, which performs owner checks and
serializes admission. A speech process restart expires active sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from metis_contracts import ErrorCode, MetisError, settings

from ..deps import Context

router = APIRouter(prefix="/v1/flow/sessions", tags=["dictation"])
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MAX_CREATE_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_TIMEOUT_SECONDS = 120


def _speech_target() -> tuple[str, int, str]:
    cfg = settings().desktop
    endpoint = cfg.speech_endpoint or "http://127.0.0.1:8787"
    secret = cfg.speech_secret or ""
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname or ""
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        port = parts.port
    except ValueError as exc:
        raise MetisError(ErrorCode.PROVIDER_UNAVAILABLE, "dictation is not configured") from exc
    if (
        parts.scheme != "http"
        or not loopback
        or not port
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or len(secret) < 32
        or not secret.isascii()
    ):
        raise MetisError(ErrorCode.PROVIDER_UNAVAILABLE, "dictation is not configured")
    return host, port, secret


def _forward(method: str, path: str, body: bytes, *, user_id: str, content_type: str) -> Response:
    host, port, secret = _speech_target()
    owner = hmac.new(secret.encode(), b"metis-flow-owner-v1\0" + user_id.encode(), hashlib.sha256).hexdigest()
    connection = http.client.HTTPConnection(host, port, timeout=_TIMEOUT_SECONDS)
    try:
        connection.request(
            method,
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {secret}",
                "X-Metis-Flow-Owner": owner,
                "Content-Type": content_type,
            },
        )
        upstream = connection.getresponse()
        data = upstream.read(_MAX_RESPONSE_BYTES + 1)
        if len(data) > _MAX_RESPONSE_BYTES:
            raise MetisError(ErrorCode.PROVIDER_UNAVAILABLE, "dictation returned an oversized response")
        return Response(
            data,
            status_code=upstream.status,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except (OSError, http.client.HTTPException) as exc:
        raise MetisError(ErrorCode.PROVIDER_UNAVAILABLE, "dictation is unavailable") from exc
    finally:
        connection.close()


def _path(session_id: str, action: str = "") -> str:
    if not _SESSION_ID.fullmatch(session_id):
        raise MetisError(ErrorCode.NOT_FOUND, "dictation session not found")
    return f"/v1/flow/sessions/{session_id}{action}"


@router.post("")
async def create_session(request: Request, ctx: Context) -> Response:
    body = await request.body()
    if len(body) > _MAX_CREATE_BYTES:
        raise MetisError(ErrorCode.UPLOAD_TOO_LARGE, "dictation settings are too large")
    return await run_in_threadpool(
        _forward, "POST", "/v1/flow/sessions", body, user_id=ctx.user_id, content_type="application/json"
    )


@router.post("/{session_id}/audio")
async def add_audio(session_id: str, request: Request, ctx: Context) -> Response:
    path = _path(session_id, "/audio")
    body = await request.body()
    return await run_in_threadpool(
        _forward, "POST", path, body, user_id=ctx.user_id, content_type="application/octet-stream"
    )


@router.post("/{session_id}/finish")
async def finish_session(session_id: str, ctx: Context) -> Response:
    return await run_in_threadpool(
        _forward,
        "POST",
        _path(session_id, "/finish"),
        b"",
        user_id=ctx.user_id,
        content_type="application/json",
    )


@router.delete("/{session_id}")
async def cancel_session(session_id: str, ctx: Context) -> Response:
    return await run_in_threadpool(
        _forward, "DELETE", _path(session_id), b"", user_id=ctx.user_id, content_type="application/json"
    )
