"""Shared request context: adapters, principal, idempotency and rate limiting.

Adapters are built once per process and injected, so a test can swap any of
them for a fixture and the whole API behaves deterministically.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from metis_contracts import ErrorCode, MetisError, SystemClock, settings
from metis_contracts.common import Record
from metis_contracts.ports import (
    Clock,
    Embedder,
    InferenceClient,
    MemoryStore,
    NotificationTransport,
    ScheduleStore,
    SourceStore,
    SpeechTranscriber,
    StateStore,
)

from .auth import Principal, principal_from_header
from .guards import clean_correlation_id
from .rate_limit import SlidingWindowRateLimiter as RateLimiter

_FINGERPRINT_FIELD = "__fingerprint"
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_.:=-]{1,128}")

log = logging.getLogger("metis.api.deps")


@dataclass
class Adapters:
    """Everything the routes are allowed to touch.

    Typed against the port protocols in `metis_contracts.ports`, so an adapter
    that drifts from its published interface fails the typecheck here instead
    of at runtime in a route.
    """

    clock: Clock
    state_store: StateStore
    source_store: SourceStore
    inference: InferenceClient
    transcriber: SpeechTranscriber | None = None
    schedule_store: ScheduleStore | None = None
    memory_store: MemoryStore | None = None
    embedder: Embedder | None = None
    notification_transports: dict[str, NotificationTransport] = field(default_factory=dict)

    @property
    def retrieval(self) -> Any:
        from metis_retrieval import RetrievalService

        if self.memory_store is None or self.embedder is None:
            raise MetisError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "memory retrieval is unavailable: the retrieval adapters are not configured",
            )
        return RetrievalService(
            state_store=self.state_store,
            memory_store=self.memory_store,
            embedder=self.embedder,
            clock=self.clock,
        )

    @property
    def planner(self) -> Any:
        from metis_planner import PlannerService

        return PlannerService(
            state_store=self.state_store,
            retrieval=self.retrieval,
            inference=self.inference,
            clock=self.clock,
        )

    def describe(self) -> dict[str, str]:
        """Adapter modes, surfaced through /v1/health so the UI can be honest."""

        def mode_of(adapter: Any) -> str:
            return str(getattr(getattr(adapter, "mode", None), "value", getattr(adapter, "mode", "none")))

        extra: dict[str, str] = {}
        if hasattr(self.inference, "health"):  # local runtime: report model + reachability
            ok, detail = self.inference.health()
            extra = {"inference_model": self.inference.model_id, "inference_runtime": detail}
            if not ok:
                extra["inference_runtime"] = f"unavailable: {detail}"
        return {
            **extra,
            "state_store": mode_of(self.state_store),
            "source_store": mode_of(self.source_store),
            "inference": mode_of(self.inference),
            "transcriber": mode_of(self.transcriber) if self.transcriber else "none",
            "schedule_store": mode_of(self.schedule_store) if self.schedule_store else "none",
            "memory_store": mode_of(self.memory_store) if self.memory_store else "none",
            "embedder": mode_of(self.embedder) if self.embedder else "none",
        }


def build_adapters() -> Adapters:
    """Wire adapters for the configured mode."""
    from metis_pipeline.inference import build_inference_client
    from metis_state import build_source_store, build_state_store

    clock = SystemClock()
    state_store = build_state_store(clock=clock)
    adapters = Adapters(
        clock=clock,
        state_store=state_store,
        source_store=build_source_store(clock=clock),
        inference=build_inference_client(),
    )
    try:
        from metis_speech import build_transcriber

        adapters.transcriber = build_transcriber()
    except ImportError:
        log.warning("metis_speech is unavailable; audio capture will report provider_unavailable")
    try:
        from metis_scheduler import build_notification_transports, build_schedule_store

        adapters.schedule_store = build_schedule_store(state_store=state_store, clock=clock)
        adapters.notification_transports = build_notification_transports(state_store, clock)
    except ImportError:
        log.warning("metis_scheduler is unavailable; reminders cannot be scheduled")
    try:
        from metis_retrieval import build_embedder, build_memory_store

        adapters.memory_store = build_memory_store()
        adapters.embedder = build_embedder()
    except ImportError:
        log.warning("metis_retrieval is unavailable; search will report degraded")
    return adapters


_limiter = RateLimiter()


@dataclass
class RequestContext:
    """One request's resolved identity, adapters and correlation id."""

    principal: Principal
    adapters: Adapters
    correlation_id: str
    idempotency_key: str | None = None
    #: Hash of method + path + body; ties a stored response to the request it answered.
    fingerprint: str | None = None

    @property
    def user_id(self) -> str:
        return self.principal.user_id

    @property
    def state(self) -> StateStore:
        return self.adapters.state_store

    def require[R: Record](self, kind: str, entity_id: str, model: type[R]) -> R:
        """Load an owned entity or raise 404.

        The kind-to-model mapping is the store's, so the isinstance check is
        real: a kind that resolves to the wrong model is a storage defect, not
        a client error.
        """
        record = self.state.get(self.user_id, kind, entity_id)
        if record is None:
            raise MetisError(ErrorCode.NOT_FOUND, f"{kind} {entity_id} not found")
        if not isinstance(record, model):
            raise MetisError(
                ErrorCode.INTERNAL,
                f"{kind} {entity_id} is stored as {type(record).__name__}, not {model.__name__}",
            )
        return record

    def throttle(self, scope: str, *, per_minute: int) -> None:
        """A tighter per-user limit for an endpoint that makes a paid call."""
        _limiter.check(f"{scope}:{self.user_id}", limit=per_minute)

    def require_idempotency_key(self) -> str:
        if not self.idempotency_key:
            raise MetisError(
                ErrorCode.VALIDATION_FAILED,
                "this operation creates work and requires an Idempotency-Key header",
            )
        return self.idempotency_key

    def replay(self) -> dict[str, Any] | None:
        """A repeated successful mutation returns its stored response."""
        if not self.idempotency_key:
            return None
        stored = self.state.recall_idempotent(self.user_id, self.idempotency_key)
        if stored is None:
            return None
        stored = dict(stored)
        if stored.pop(_FINGERPRINT_FIELD, None) not in (None, self.fingerprint):
            # Replaying the first response here would silently drop this request.
            raise MetisError(
                ErrorCode.VALIDATION_FAILED, "this Idempotency-Key was already used for a different request"
            )
        return stored

    def remember(self, response: dict[str, Any]) -> None:
        if self.idempotency_key:
            self.state.remember_idempotent(
                self.user_id, self.idempotency_key, {**response, _FINGERPRINT_FIELD: self.fingerprint}
            )


#: Starlette's TestClient reports "testclient"; a real socket peer is always an IP.
_TEST_CLIENT = "testclient"


def _is_loopback_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host == _TEST_CLIENT:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def get_adapters(request: Request) -> Adapters:
    adapters = getattr(request.app.state, "adapters", None)
    if not isinstance(adapters, Adapters):
        raise MetisError(ErrorCode.INTERNAL, "the application adapters are not initialised")
    return adapters


async def get_context(
    request: Request,
    adapters: Annotated[Adapters, Depends(get_adapters)],
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    x_correlation_id: Annotated[str | None, Header(alias="X-Correlation-Id")] = None,
) -> RequestContext:
    fingerprint: str | None = None
    principal = principal_from_header(authorization)
    if principal.is_fixture and not _is_loopback_client(request):
        # The demo identity needs no credential, so it is only for this machine.
        # The Host guard stops DNS rebinding, but a LAN caller can send any Host.
        raise MetisError(ErrorCode.UNAUTHENTICATED, "authentication is required")
    limits = settings().limits
    _limiter.check_all(
        [
            (f"user:{principal.user_id}", limits.api_rate_per_minute, 60),
            (f"user:{principal.user_id}", limits.api_rate_per_hour, 3600),
            ("global", limits.api_rate_global_per_minute, 60),
        ]
    )
    correlation_id = clean_correlation_id(x_correlation_id)
    if idempotency_key is not None:
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise MetisError(ErrorCode.VALIDATION_FAILED, "Idempotency-Key must be 1-128 of [A-Za-z0-9_.:=-]")
        body = await request.body()
        fingerprint = hashlib.sha256(
            request.method.encode() + b"\0" + request.url.path.encode() + b"\0" + body
        ).hexdigest()
    request.state.correlation_id = correlation_id
    request.state.user_id = principal.user_id
    return RequestContext(
        principal=principal,
        adapters=adapters,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
    )


Context = Annotated[RequestContext, Depends(get_context)]
