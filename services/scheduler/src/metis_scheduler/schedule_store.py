"""ScheduleStore adapter: a timer table on the state store.

A database write and a scheduler change cannot be one transaction, so this is
always driven by the outbox dispatcher. Two consequences are designed in rather
than wished away:

* A successful entity save with a failed schedule create is
  ``pending_schedule`` or ``schedule_failed`` — never "reminder set".
* A superseded schedule may still fire, so the due handler re-validates
  freshness itself.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from metis_contracts import AdapterMode, ErrorCode, MetisError
from metis_contracts.ports import Clock, StateStore


def schedule_name(reminder_id: str) -> str:
    """Stable provider-side name, so create is idempotent on the reminder id."""
    return f"metis-rem-{reminder_id}"


def daily_schedule_name(user_id: str) -> str:
    """One stable name per user; hashed because subjects may hold characters
    the provider forbids."""
    return f"metis-daily-{hashlib.sha256(user_id.encode()).hexdigest()[:40]}"


def parse_local_time(local_time: str, timezone: str) -> tuple[time, ZoneInfo]:
    try:
        return time.fromisoformat(local_time), ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError, KeyError) as exc:
        raise MetisError(ErrorCode.VALIDATION_FAILED, f"invalid daily time/timezone: {exc}") from exc


class LocalScheduleStore:
    """Local mode: the reminder row's own ``trigger_at`` is the schedule.

    ``due_reminder_ids`` on the state store is the timer. Nothing external is
    created, so there is nothing to leak between dev runs.
    """

    mode = AdapterMode.LOCAL

    def __init__(self, state_store: StateStore | None = None, clock: Clock | None = None) -> None:
        self._state = state_store
        self._clock = clock
        self.created: dict[str, datetime] = {}
        self.deleted: list[str] = []
        #: user_id -> (local_time, timezone, created_at, payload)
        self.daily: dict[str, tuple[str, str, datetime, dict[str, Any]]] = {}

    def _now(self) -> datetime:
        return self._clock.now() if self._clock else datetime.now(UTC)

    def upsert_daily(self, *, user_id: str, local_time: str, timezone: str, payload: dict[str, Any]) -> str:
        parse_local_time(local_time, timezone)
        old = self.daily.get(user_id)
        # Retrying the same settings keeps the original creation time, so it
        # cannot re-arm an occurrence that already passed.
        created = old[2] if old and old[:2] == (local_time, timezone) else self._now()
        self.daily[user_id] = (local_time, timezone, created, payload)
        return f"local:{daily_schedule_name(user_id)}"

    def delete_daily(self, *, user_id: str, scheduler_ref: str | None) -> None:
        self.daily.pop(user_id, None)

    def list_due_daily(self, *, now: datetime, limit: int = 50) -> list[tuple[str, str]]:
        """Today's occurrence (user local date) once its instant has passed and
        the schedule existed before it. Callers dedupe on (user, date).

        The instant is local wall-clock time in the IANA zone: a time skipped by
        spring-forward fires once, shifted by the gap; an ambiguous fall-back
        time fires at its first occurrence.
        """
        due: list[tuple[str, str]] = []
        for user_id, (local_time, tz, created, _payload) in self.daily.items():
            at, zone = parse_local_time(local_time, tz)
            today: date = now.astimezone(zone).date()
            fire = datetime.combine(today, at, tzinfo=zone).astimezone(UTC)
            if created <= fire <= now:
                due.append((user_id, today.isoformat()))
        return due[:limit]

    def upsert_schedule(
        self, *, reminder_id: str, user_id: str, trigger_at: datetime, payload: dict[str, Any]
    ) -> str:
        self.created[reminder_id] = trigger_at
        self.deleted = [d for d in self.deleted if d != reminder_id]
        return f"local:{schedule_name(reminder_id)}"

    def delete_schedule(self, *, reminder_id: str, scheduler_ref: str | None) -> None:
        self.created.pop(reminder_id, None)
        self.deleted.append(reminder_id)

    def list_due(self, *, now: datetime, limit: int = 50) -> list[str]:
        if self._state is None:
            return [rid for rid, when in self.created.items() if when <= now][:limit]
        return self._state.due_reminder_ids(now=now, limit=limit)


def build_schedule_store(state_store: Any | None = None, clock: Any | None = None) -> Any:
    return LocalScheduleStore(state_store=state_store, clock=clock)
