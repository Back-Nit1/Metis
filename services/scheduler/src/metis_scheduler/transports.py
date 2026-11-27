"""NotificationTransport adapters.

The in-app feed is the delivery channel; a reminder is written there and is
never presented as "the user was notified" beyond that record.
"""

from __future__ import annotations

from typing import Any

from metis_contracts import DeliveryStatus, Notification
from metis_contracts.enums import AdapterMode


class InAppTransport:
    """Writes to the user's own notification feed; always supported."""

    channel = "in_app"

    def __init__(self, state_store: Any, clock: Any) -> None:
        self.mode = getattr(state_store, "mode", AdapterMode.LOCAL)
        self._state = state_store
        self._clock = clock

    def supported(self, user_id: str) -> bool:
        return True

    def send(self, notification: Notification) -> tuple[DeliveryStatus, str | None]:
        # The record IS the delivery for this channel; the caller persists it in
        # the same transaction that claims the occurrence.
        return DeliveryStatus.ACCEPTED, "written to the in-app feed"


class FixtureTransport:
    """Collects sends in memory for tests and local demos."""

    channel = "fixture"
    mode = AdapterMode.FIXTURE

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def supported(self, user_id: str) -> bool:
        return True

    def send(self, notification: Notification) -> tuple[DeliveryStatus, str | None]:
        self.sent.append(notification)
        return DeliveryStatus.ACCEPTED, "fixture transport (not a real delivery)"


def build_notification_transports(state_store: Any, clock: Any) -> dict[str, Any]:
    """Channel -> transport. The in-app feed is always present."""
    return {"in_app": InAppTransport(state_store, clock)}
