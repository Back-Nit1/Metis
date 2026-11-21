"""In-process sliding-window request limiting.

The edge limiter protects the API before authentication.  Forwarded client
addresses are only considered when the socket peer is a configured trusted
proxy; otherwise an attacker could choose a fresh identity on every request.

This implementation is intentionally process-local.  It protects the current
single-process VM deployment.  A multi-instance deployment must put the same
policy in a shared gateway/store so every replica observes one window.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any

from metis_contracts import ErrorCode, MetisError

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

_DEFAULT_LIMIT = 100
_DEFAULT_WINDOW_SECONDS = 60
_EXEMPT_PATHS = frozenset({"/health", "/ready", "/readiness", "/v1/health", "/v1/ready", "/v1/readiness"})
_DEFAULT_TRUSTED_PROXIES = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


@dataclass(frozen=True)
class RateLimitDecision:
    """State of one admitted request after it has consumed a slot."""

    limit: int
    remaining: int
    reset_after_seconds: int
    window_seconds: int


class SlidingWindowRateLimiter:
    """Exact rolling-window limiter using one timestamp per admitted request.

    All rules are checked under one lock before any timestamp is appended, so
    a request refused by one rule consumes no capacity from the other rules.
    """

    def __init__(self, *, max_keys: int = 50_000) -> None:
        self._events: dict[tuple[str, int], deque[float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys
        self._last_cleanup = 0.0

    def check_all(self, rules: Sequence[tuple[str, int, int]]) -> list[RateLimitDecision]:
        """Admit one request against ``(key, limit, window_seconds)`` rules."""
        now = time.monotonic()
        normalized: list[tuple[tuple[str, int], int, int, deque[float]]] = []
        with self._lock:
            self._cleanup(now)
            for key, limit, seconds in rules:
                if limit < 1 or seconds < 1:
                    raise ValueError("rate-limit values must be positive")
                slot = (key, seconds)
                events = self._events.get(slot)
                if events is None:
                    if len(self._events) >= self._max_keys:
                        raise MetisError(
                            ErrorCode.RATE_LIMITED,
                            "rate limiter capacity reached",
                            retry_after_seconds=1,
                        )
                    events = deque()
                    self._events[slot] = events
                cutoff = now - seconds
                while events and events[0] <= cutoff:
                    events.popleft()
                normalized.append((slot, limit, seconds, events))

            for _, limit, seconds, events in normalized:
                if len(events) >= limit:
                    retry_after = max(1, math.ceil(events[0] + seconds - now))
                    raise MetisError(
                        ErrorCode.RATE_LIMITED,
                        "too many requests",
                        retry_after_seconds=retry_after,
                    )

            decisions: list[RateLimitDecision] = []
            for slot, limit, seconds, events in normalized:
                events.append(now)
                reset_after = max(1, math.ceil(events[0] + seconds - now))
                decisions.append(
                    RateLimitDecision(
                        limit=limit,
                        remaining=limit - len(events),
                        reset_after_seconds=reset_after,
                        window_seconds=seconds,
                    )
                )
                self._events[slot] = events
            return decisions

    def check(self, key: str, *, limit: int, window_seconds: int = 60) -> RateLimitDecision:
        return self.check_all([(key, limit, window_seconds)])[0]

    def _cleanup(self, now: float) -> None:
        """Discard expired keys periodically so spoofed clients cannot leak RAM."""
        if now - self._last_cleanup < _DEFAULT_WINDOW_SECONDS and len(self._events) < self._max_keys:
            return
        for slot, events in list(self._events.items()):
            cutoff = now - slot[1]
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                del self._events[slot]
        self._last_cleanup = now


class ConnectionRateLimitMiddleware:
    """Limit each safely resolved network client to 100 requests per minute."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app
        self.limit = _positive_env("METIS_CONNECT_RATE_PER_MINUTE", _DEFAULT_LIMIT)
        self.window_seconds = _positive_env("METIS_CONNECT_RATE_WINDOW_SECONDS", _DEFAULT_WINDOW_SECONDS)
        self.trusted_proxies = _trusted_proxy_networks()
        self.limiter = SlidingWindowRateLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        identity = _client_identity(scope, self.trusted_proxies)
        try:
            decision = self.limiter.check(
                f"client:{identity}", limit=self.limit, window_seconds=self.window_seconds
            )
        except MetisError as exc:
            await _send_rate_limited(scope, send, exc, self.limit, self.window_seconds)
            return

        async def with_rate_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(_rate_headers(decision))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, with_rate_headers)


def _positive_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw = os.environ.get("METIS_TRUSTED_PROXY_CIDRS", "").strip()
    if not raw:
        return _DEFAULT_TRUSTED_PROXIES
    try:
        return tuple(
            ipaddress.ip_network(item.strip(), strict=False) for item in raw.split(",") if item.strip()
        )
    except ValueError as exc:
        raise ValueError("METIS_TRUSTED_PROXY_CIDRS must contain comma-separated IP networks") from exc


def _is_trusted(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def _client_identity(
    scope: Scope,
    trusted_proxies: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str:
    """Return a non-spoofable client key for the configured proxy chain."""
    peer = (scope.get("client") or ("unknown", 0))[0]
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return str(peer)
    if not _is_trusted(peer_address, trusted_proxies):
        return peer_address.compressed

    raw_headers = scope.get("headers", [])
    forwarded = next(
        (value.decode("latin-1") for name, value in raw_headers if name.lower() == b"x-forwarded-for"),
        "",
    )
    chain: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        chain = [ipaddress.ip_address(item.strip()) for item in forwarded.split(",") if item.strip()]
    except ValueError:
        # A malformed chain is attacker-controlled input.  Collapse it to the
        # trusted proxy rather than accepting a user-selected key.
        return peer_address.compressed

    # Work from the nearest hop backwards.  This ignores forged leftmost
    # entries while allowing explicitly trusted multi-proxy deployments.
    for address in reversed(chain):
        if not _is_trusted(address, trusted_proxies):
            return address.compressed
    return chain[0].compressed if chain else peer_address.compressed


def _rate_headers(decision: RateLimitDecision) -> list[tuple[bytes, bytes]]:
    return [
        (b"ratelimit-limit", str(decision.limit).encode()),
        (b"ratelimit-remaining", str(decision.remaining).encode()),
        (b"ratelimit-reset", str(decision.reset_after_seconds).encode()),
        (b"ratelimit-policy", f"{decision.limit};w={decision.window_seconds}".encode()),
    ]


async def _send_rate_limited(
    scope: Scope,
    send: Send,
    exc: MetisError,
    limit: int,
    window_seconds: int,
) -> None:
    retry_after = exc.retry_after_seconds or 1
    correlation_id = (scope.get("state") or {}).get("correlation_id")
    body = json.dumps(exc.body(correlation_id).model_dump(mode="json"), separators=(",", ":")).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"retry-after", str(retry_after).encode()),
        (b"ratelimit-limit", str(limit).encode()),
        (b"ratelimit-remaining", b"0"),
        (b"ratelimit-reset", str(retry_after).encode()),
        (b"ratelimit-policy", f"{limit};w={window_seconds}".encode()),
    ]
    await send({"type": "http.response.start", "status": 429, "headers": headers})
    await send({"type": "http.response.body", "body": body})
