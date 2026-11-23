"""Local worker: outbox dispatch plus the local due-reminder/plan/sync timers.

In the cloud these are separate things — a Lambda consumes SQS for the outbox,
and EventBridge Scheduler invokes the due handler directly. Locally the worker
polls both so `make dev` needs no AWS account.

    python -m metis_pipeline.local_worker [--once] [--interval 2] [--processes N]

``--processes 1`` is one loop over every lane. ``N >= 2`` (D9) supervises N
spawned children, each with its own SQLite connection: child 0 runs the
interactive lane plus the timers, children 1..N-1 run the ingest lane under the
global ``ingest_max_in_flight`` cap, so a 10k-file import cannot starve
reminders. The parent only supervises; it emits the readiness record once all
children are wired, forwards stop, and joins them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from types import FrameType
from typing import Any, Literal

from metis_contracts import SystemClock, settings

log = logging.getLogger("metis.pipeline.worker")

#: Bump when the readiness record's shape changes; the desktop supervisor checks it.
READINESS_PROTOCOL = 1

_stop = False
_stop_event = threading.Event()

#: How long the parent waits for every child to wire its adapters.
CHILD_READY_TIMEOUT_SECONDS = 120.0
#: How long a child gets to finish its batch after stop, before terminate().
CHILD_JOIN_TIMEOUT_SECONDS = 30.0

Lane = Literal["all", "interactive", "ingest"]


def request_stop() -> None:
    """Finish the current batch, then stop (all children too, in supervisor mode)."""
    global _stop
    _stop = True
    _stop_event.set()


def _request_stop(signum: int, _frame: FrameType | None) -> None:
    log.info("received signal %s; finishing the current batch then stopping", signum)
    request_stop()


def _configure_logging() -> None:
    logging.basicConfig(level=settings().log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def build_context() -> Any:
    """Wire the adapters for the configured mode. Fixtures stay clearly labelled."""
    from metis_state import build_source_store, build_state_store

    from .dispatcher import DispatchContext
    from .inference import build_inference_client

    clock = SystemClock()
    state_store = build_state_store(clock=clock)
    context = DispatchContext(
        state_store=state_store,
        source_store=build_source_store(clock=clock),
        clock=clock,
        inference=build_inference_client(),
    )

    from .transcription import build_asr_launcher

    context.asr_launcher = build_asr_launcher()  # None outside cloud mode

    try:
        from metis_speech import build_transcriber

        context.transcriber = build_transcriber()
    except ImportError:
        log.warning("metis_speech is not installed; audio captures cannot be transcribed")

    try:
        from metis_scheduler import build_notification_transports, build_schedule_store

        context.schedule_store = build_schedule_store(clock=clock)
        context.notification_transports = build_notification_transports(state_store, clock=clock)
    except ImportError:
        log.warning("metis_scheduler is not installed; reminders will not be dispatched")

    try:
        from metis_retrieval import build_embedder, build_memory_store

        context.memory_store = build_memory_store()
        context.embedder = build_embedder()
    except ImportError:
        log.warning("metis_retrieval is not installed; semantic indexing is disabled")

    return context


def build_registry() -> Any:
    """Merge every service's handlers into one registry.

    Each service owns its own handlers; the worker only composes them, so two
    services can never register the same event type without failing loudly.
    """
    from .dispatcher import HandlerRegistry
    from .handlers import registry as pipeline_registry

    registry = HandlerRegistry()
    registry.merge(pipeline_registry)

    try:
        from metis_scheduler.handlers import registry as scheduler_registry

        registry.merge(scheduler_registry)
    except ImportError:
        pass

    try:
        from metis_retrieval.handlers import registry as retrieval_registry

        registry.merge(retrieval_registry)
    except ImportError:
        pass

    try:
        from metis_planner.handlers import registry as planner_registry

        registry.merge(planner_registry)
    except ImportError:
        pass

    return registry


def emit_readiness() -> None:
    """One JSON line on stdout once adapters and handlers are wired, before the loop.

    Logs go to stderr; stdout carries only this record so a supervisor can read it.
    """
    record = {"event": "started", "service": "worker", "protocol": READINESS_PROTOCOL, "pid": os.getpid()}
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _loop(
    context: Any,
    dispatcher: Any,
    *,
    timers: bool,
    interval: float,
    once: bool,
    stopped: Callable[[], bool],
    wait: Callable[[float], object],
) -> int:
    idle_cycles = 0
    while True:
        result = dispatcher.run_once()
        due = (
            _fire_due_reminders(context) + _fire_due_daily_plans(context) + _fire_due_syncs(context)
            if timers
            else 0
        )
        if result.processed or due:
            idle_cycles = 0
            log.info(
                "batch lane=%s processed=%d ok=%d superseded=%d retried=%d unhandled=%d dead=%d due=%d",
                dispatcher.lane,
                result.processed,
                result.succeeded,
                result.superseded,
                result.retried,
                result.unhandled,
                result.dead_lettered,
                due,
            )
        else:
            idle_cycles += 1
        if once or stopped():
            return 0
        # Back off while idle so a dev loop is not a busy spin.
        wait(min(interval * (2 if idle_cycles > 5 else 1), 10.0))
        if stopped():
            return 0


def _wire(lane: Lane) -> tuple[Any, Any]:
    from .dispatcher import OutboxDispatcher

    context = build_context()
    context.worker_id = f"{context.worker_id}:{os.getpid()}:{lane}"
    registry = build_registry()
    dispatcher = OutboxDispatcher(registry, context, lane=lane)
    log.info(
        "worker started mode=%s lane=%s handlers=%d worker_id=%s",
        settings().mode.value,
        lane,
        len(registry.event_types),
        context.worker_id,
    )
    return context, dispatcher


def run(*, once: bool = False, interval: float = 2.0, processes: int = 1) -> int:
    _configure_logging()
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    if processes >= 2 and not once:
        return _supervise(processes, interval)

    context, dispatcher = _wire("all")
    emit_readiness()
    return _loop(
        context,
        dispatcher,
        timers=True,
        interval=interval,
        once=once,
        stopped=lambda: _stop,
        wait=_stop_event.wait,
    )


def _supervise(processes: int, interval: float) -> int:
    import multiprocessing

    mp = multiprocessing.get_context("spawn")
    shared_stop = mp.Event()
    children: list[tuple[Any, Any]] = []
    deadline = time.monotonic() + CHILD_READY_TIMEOUT_SECONDS

    def start(index: int) -> None:
        lane: Lane = "interactive" if index == 0 else "ingest"
        ready = mp.Event()
        proc = mp.Process(
            target=_child_main,
            args=(index, lane, interval, shared_stop, ready, os.getpid()),
            name=f"metis-worker-{index}-{lane}",
        )
        proc.start()
        children.append((proc, ready))

    def all_ready() -> bool:
        while not _stop and not all(ready.is_set() for _, ready in children):
            if any(not proc.is_alive() for proc, _ in children) or time.monotonic() > deadline:
                log.error("a worker process failed to start; stopping the rest")
                return False
            _stop_event.wait(0.1)
        return not _stop

    # Child 0 alone first: it creates/migrates the SQLite files, so siblings
    # never race a fresh database's WAL switch.
    start(0)
    ok = all_ready()
    if ok:
        for index in range(1, processes):
            start(index)
        ok = all_ready()

    rc = 0
    if ok:
        log.info("supervising %d worker processes: %s", processes, [p.name for p, _ in children])
        emit_readiness()
        while not _stop:
            dead = [proc.name for proc, _ in children if not proc.is_alive()]
            if dead:
                log.error("worker process exited unexpectedly: %s; stopping the rest", dead)
                rc = 1
                break
            _stop_event.wait(1.0)
    elif not _stop:
        rc = 1

    shared_stop.set()
    for proc, _ in children:
        proc.join(CHILD_JOIN_TIMEOUT_SECONDS)
        if proc.is_alive():
            log.warning("%s did not stop in time; terminating", proc.name)
            proc.terminate()
            proc.join(5)
    return rc


def _child_main(
    index: int, lane: Lane, interval: float, shared_stop: Any, ready: Any, parent_pid: int
) -> None:
    """Entry point of one spawned worker. Builds its own context and connection."""
    _configure_logging()
    signal.signal(signal.SIGTERM, _request_stop)
    # Ctrl-C reaches the whole process group; the parent coordinates the stop.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    def stopped() -> bool:
        return _stop or shared_stop.is_set() or os.getppid() != parent_pid

    def wait(seconds: float) -> None:
        # Short slices so an orphaned child notices its parent died.
        end = time.monotonic() + seconds
        while not stopped() and (remaining := end - time.monotonic()) > 0:
            shared_stop.wait(min(remaining, 0.5))

    context, dispatcher = _wire(lane)
    ready.set()
    _loop(context, dispatcher, timers=index == 0, interval=interval, once=False, stopped=stopped, wait=wait)
    log.info("worker process %d (%s) stopped", index, lane)


def _fire_due_reminders(context: Any) -> int:
    """Local stand-in for EventBridge Scheduler.

    It only *raises* the due event; the scheduler's own handler still performs
    the full freshness and quiet-hours validation, so the local and cloud paths
    exercise the same suppression logic.
    """
    state = context.state_store
    lister = getattr(state, "due_reminder_ids", None)
    if lister is None:
        return 0
    try:
        from metis_scheduler.service import raise_due_event
    except ImportError:
        return 0

    fired = 0
    for composite in lister(now=context.clock.now(), limit=25):
        user_id, _, reminder_id = composite.partition("/")
        if raise_due_event(state, user_id=user_id, reminder_id=reminder_id, clock=context.clock):
            fired += 1
    return fired


def _fire_due_daily_plans(context: Any) -> int:
    """Local stand-in for the EventBridge daily-plan schedule.

    metis_api owns the due logic (freshness, dedupe, plan.requested). The API
    depends on this package, so it is imported lazily here, as the worker does
    for the other services; absent API or schedule store means nothing to do.
    """
    if context.schedule_store is None:
        return 0
    try:
        from metis_api.planning import run_local_daily_due
    except ImportError:
        return 0
    results = run_local_daily_due(context.state_store, context.clock, context.schedule_store)
    return sum(r["enqueued"] for r in results)


def _fire_due_syncs(context: Any) -> int:
    """Re-sync connectors whose interval has passed (D10). metis_retrieval owns
    the logic; absent or older retrieval means nothing to do."""
    import importlib

    try:
        enqueue_due_syncs = importlib.import_module("metis_retrieval.connectors").enqueue_due_syncs
    except (ImportError, AttributeError):
        return 0
    try:
        return int(enqueue_due_syncs(context.state_store, context.clock))
    except Exception:
        # A sync-timer bug must not take reminders down with it.
        log.exception("enqueue_due_syncs failed; will retry next cycle")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Metis local worker loop")
    parser.add_argument("--once", action="store_true", help="process one batch in-process then exit")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between polls")
    parser.add_argument(
        "--processes",
        type=int,
        default=None,
        help="1 = one loop over all lanes; N>=2 = 1 interactive + N-1 ingest processes "
        "(default: METIS_WORKER_PROCESSES)",
    )
    args = parser.parse_args(argv)
    processes = args.processes if args.processes is not None else settings().limits.worker_processes
    return run(once=args.once, interval=args.interval, processes=max(1, processes))


if __name__ == "__main__":
    sys.exit(main())
