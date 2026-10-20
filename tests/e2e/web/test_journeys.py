"""First-time-user journeys. See conftest.py for target/matrix rules."""

from __future__ import annotations

import re
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect

MULTI = (
    "Dentist next Tuesday at 3. Register for the Boston half marathon by March 3rd. "
    "Remind me to call mom tomorrow at 6pm. Buy milk."
)


def uid() -> str:
    return uuid.uuid4().hex[:8]


def enter(page: Page, stack, route: str = "today") -> None:
    page.goto(stack.web + "/")
    page.get_by_role("button", name="Open Metis").first.click()
    page.goto(f"{stack.web}/#/{route}")


def send(page: Page, text: str) -> None:
    box = page.locator("textarea").first
    box.fill(text)
    page.get_by_role("button", name="Send note").click()


def seed_task(api, title: str, priority: int = 1) -> dict:
    r = api.post(
        "/v1/tasks",
        data={"title": title, "state": "ready", "priority": priority},
        headers={"Idempotency-Key": uid()},
    )
    assert r.ok, r.text()
    return r.json()


# --- first visit ------------------------------------------------------------


@pytest.mark.ci_safe
def test_first_visit_open_metis_lands_on_today(page, stack, probe):
    page.goto(stack.web + "/")
    expect(page.get_by_role("button", name="Open Metis").first).to_be_visible()
    page.get_by_role("button", name="Open Metis").first.click()
    expect(page.get_by_text("Plan my day")).to_be_visible()
    assert not probe["console"], probe["console"]


@pytest.mark.ci_safe
def test_fixture_mode_is_labelled(page, stack, probe):
    if "localhost" not in stack.web:
        pytest.skip("local fixture labelling only")
    enter(page, stack)
    page.wait_for_timeout(500)
    # rail on desktop, header chip on tablet/phone
    assert any(el.is_visible() for el in page.get_by_text("Fixture mode").all()), "no visible fixture label"


# --- capture -> review -> save ----------------------------------------------


@pytest.mark.mutating
@pytest.mark.ci_safe
def test_typed_note_review_save_reaches_tasks(page, stack, probe, api):
    marker = uid()
    enter(page, stack, "capture")
    send(page, f"Next I want to test diff invalidation {marker}.")
    expect(page.get_by_role("button", name=re.compile(r"Save \d+ item"))).to_be_visible(timeout=15000)
    page.get_by_role("button", name=re.compile(r"Save \d+ item")).click()
    expect(page.get_by_text("Saved. The records below were created.")).to_be_visible()
    page.goto(f"{stack.web}/#/tasks")
    expect(page.get_by_text("Test the next approach").first).to_be_visible()
    assert not probe["console"], probe["console"]


@pytest.mark.mutating
@pytest.mark.known_issue(
    owner="A", note="multi-item note yields one proposal (fixture is rule-based; recheck on deployed)"
)
def test_multi_item_note_yields_four_proposals(page, stack, probe):
    enter(page, stack, "capture")
    send(page, MULTI)
    expect(page.get_by_text(re.compile(r"\d+ found"))).to_be_visible(timeout=15000)
    assert "4 found" in page.inner_text("main"), page.inner_text("main")[:300]


@pytest.mark.mutating
@pytest.mark.known_issue(owner="A", note="typed note shows a Transcribing step")
def test_typed_note_has_no_transcribing_step(page, stack, probe):
    enter(page, stack, "capture")
    send(page, f"Buy oat milk {uid()}")
    expect(page.get_by_role("list", name="Progress")).to_be_visible(timeout=15000)
    assert "Transcribing" not in page.get_by_role("list", name="Progress").inner_text()


@pytest.mark.mutating
@pytest.mark.known_issue(owner="B", note="raw src_/extract- ids are shown to users")
def test_no_raw_ids_in_ui(page, stack, probe):
    enter(page, stack, "capture")
    send(page, f"Next I want to test raw ids {uid()}")
    expect(page.get_by_text(re.compile(r"\d+ found"))).to_be_visible(timeout=15000)
    page.get_by_role("button", name=re.compile(r"Save \d+ item")).click()
    expect(page.get_by_text("Saved.")).to_be_visible()
    page.goto(f"{stack.web}/#/tasks")
    page.get_by_text("Test the next approach").first.click()
    body = page.inner_text("main")
    assert not re.search(r"\b(src|extract|task|cap)_[0-9a-f]{6,}|\bsrc_\w+|extract-\d+", body), body[:400]


# --- tasks ------------------------------------------------------------------


def _task_card(page, title):
    return page.get_by_role("listitem").filter(has_text=title)


@pytest.mark.mutating
@pytest.mark.ci_safe
def test_complete_task(page, stack, probe, api):
    t = f"finish-{uid()}"
    seed_task(api, t)
    enter(page, stack, "tasks")
    card = _task_card(page, t)
    card.get_by_role("button", name="Complete", exact=True).click()
    expect(card.get_by_role("button", name="Complete", exact=True)).to_have_count(0)
    assert api.get("/v1/tasks?state=done").text().count(t) >= 1


@pytest.mark.mutating
def test_cancel_task(page, stack, probe, api):
    t = f"drop-{uid()}"
    seed_task(api, t)
    enter(page, stack, "tasks")
    _task_card(page, t).get_by_role("button", name="Cancel", exact=True).click()
    expect(_task_card(page, t).get_by_role("button", name="Cancel", exact=True)).to_have_count(0)


@pytest.mark.mutating
def test_defer_task_from_today(page, stack, probe, api):
    """Defer lives on Today ("Defer to inbox"), not on the Tasks list (see report: polish)."""
    # Today lists only the top-N focus tasks: cancel earlier open tasks so ours is always shown.
    for old in api.get("/v1/tasks?limit=200").json()["items"]:
        if old["state"] in ("inbox", "ready", "in_progress", "blocked"):
            api.patch(
                f"/v1/tasks/{old['id']}", data={"expected_revision": old["revision"], "state": "canceled"}
            )
    t = seed_task(api, f"later-{uid()}", priority=3)
    enter(page, stack, "today")
    card = page.get_by_role("listitem").filter(has_text=t["title"])
    card.get_by_role("button", name="Defer to inbox").click()
    deadline = time.time() + 10
    while time.time() < deadline and api.get(f"/v1/tasks/{t['id']}").json()["state"] != "inbox":
        page.wait_for_timeout(300)
    assert api.get(f"/v1/tasks/{t['id']}").json()["state"] == "inbox"


# --- alerts / reminders -----------------------------------------------------


@pytest.mark.mutating
@pytest.mark.ci_safe
def test_due_reminder_appears_in_alerts(page, stack, probe, api):
    """Local worker fires on the real clock: trigger 3s ahead (no server-side clock override exists)."""
    # The server honours quiet hours (22:00-08:00 by default) and would defer the alert: clear them for determinism.
    prefs = api.get("/v1/preferences").json()
    api.patch("/v1/preferences", data={"expected_revision": prefs["revision"], "quiet_hours": []})
    t = seed_task(api, f"remind-{uid()}")
    due = datetime.now(UTC) + timedelta(seconds=3)
    r = api.post(
        "/v1/reminders",
        data={
            "target_id": t["id"],
            "target_kind": "task",
            "expected_target_revision": t["revision"],
            "trigger_at": due.isoformat(),
            "timezone": "America/New_York",
        },
        headers={"Idempotency-Key": uid()},
    )
    assert r.ok, r.text()
    enter(page, stack, "alerts")
    deadline = time.time() + 25
    while time.time() < deadline:
        page.get_by_role("button", name="Refresh").click()
        if page.get_by_text(re.compile("remind-", re.I)).count():
            break
        page.wait_for_timeout(1000)
    expect(page.get_by_text(re.compile("remind-", re.I)).first).to_be_visible()


@pytest.mark.known_issue(owner="B", note="#/notifications silently renders Today")
def test_notifications_route_is_not_today(page, stack, probe):
    enter(page, stack, "notifications")
    page.wait_for_timeout(500)
    assert "Alerts" in page.inner_text("main") and "Plan my day" not in page.inner_text("main")


# --- settings ---------------------------------------------------------------


@pytest.mark.mutating
@pytest.mark.ci_safe
def test_settings_save_and_stale_revision_conflict(page, stack, probe, api):
    enter(page, stack, "settings")
    field = page.get_by_label("Focus tasks per day")
    field.fill("4")
    page.get_by_role("button", name="Save", exact=False).first.click()
    expect(page.get_by_role("status").filter(has_text=re.compile("sav", re.I)).first).to_be_visible()
    # Another device changes preferences; this tab now holds a stale revision.
    prefs = api.get("/v1/preferences").json()
    assert api.patch(
        "/v1/preferences", data={"expected_revision": prefs["revision"], "max_focus_tasks": 5}
    ).ok
    field.fill("6")
    page.get_by_role("button", name="Save", exact=False).first.click()
    expect(page.get_by_text("changed elsewhere")).to_be_visible()
    assert api.get("/v1/preferences").json()["max_focus_tasks"] == 5, "stale write must not overwrite"
    assert any(h.startswith("409") for h in probe["http"])
    probe["http"].clear()  # the 409 is the expected outcome here


@pytest.mark.mutating
def test_export_request(page, stack, probe):
    enter(page, stack, "settings")
    page.get_by_role("button", name=re.compile("Request export|Export again")).click()
    expect(page.get_by_text(re.compile("Ready|Working on it|Checking the export"))).to_be_visible(
        timeout=20000
    )
    expect(page.get_by_text(re.compile(r"Ready.*Verified by the server"))).to_be_visible(timeout=30000)


# --- offline / reload / double click ----------------------------------------


@pytest.mark.mutating
@pytest.mark.ci_safe
def test_offline_capture_replays_once(page, stack, probe, api):
    marker = f"offline-{uid()}"
    enter(page, stack, "capture")
    page.wait_for_load_state("networkidle")
    page.context.set_offline(True)
    send(page, f"Next I want to test {marker}.")
    expect(page.get_by_text("held on this device", exact=False).first).to_be_visible(timeout=10000)
    page.context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(page.get_by_role("button", name=re.compile(r"Save \d+ item"))).to_be_visible(timeout=30000)
    caps = api.get("/v1/captures?limit=100").json()
    hits = [c for c in (caps.get("items") or caps.get("captures") or []) if marker in str(c)]
    assert len(hits) == 1, f"expected 1 capture, got {len(hits)}"
    probe["failed"].clear()  # failures while offline are the point of the test
    probe["console"].clear()


@pytest.mark.mutating
@pytest.mark.ci_safe
@pytest.mark.known_issue(
    owner="B", note="NEW: Send has no in-flight guard and mints a fresh client_capture_id per click"
)
def test_double_click_send_makes_one_capture(page, stack, probe, api):
    marker = f"dbl-{uid()}"
    enter(page, stack, "capture")
    page.locator("textarea").first.fill(f"Next I want to test {marker}.")
    page.get_by_role("button", name="Send note").dblclick()
    expect(page.get_by_role("button", name=re.compile(r"Save \d+ item"))).to_be_visible(timeout=15000)
    caps = api.get("/v1/captures?limit=100").json()
    hits = [c for c in (caps.get("items") or caps.get("captures") or []) if marker in str(c)]
    assert len(hits) == 1, f"expected 1 capture, got {len(hits)}"


@pytest.mark.mutating
def test_reload_mid_extraction_recovers(page, stack, probe, api):
    marker = f"reload-{uid()}"
    enter(page, stack, "capture")
    send(page, f"Next I want to test {marker}.")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)
    caps = api.get("/v1/captures?limit=100").json()
    hits = [c for c in (caps.get("items") or caps.get("captures") or []) if marker in str(c)]
    assert len(hits) <= 1, "reload must not duplicate the capture"
    # A first-time user must be able to find their note again: Inbox or capture review.
    visible = page.inner_text("main")
    assert "Review what was found" in visible or marker in visible or hits, "note vanished after reload"


# --- limits / permissions ---------------------------------------------------


def test_audio_over_25mb_rejected_clearly(api, stack):
    """No file picker exists in the UI (recording only), so the limit is checked at the API."""
    if stack.api == "":
        pytest.skip("needs METIS_API_URL")
    r = api.post(
        "/v1/captures",
        data={
            "kind": "audio",
            "capture_timezone": "America/New_York",
            "captured_at": datetime.now(UTC).isoformat(),
            "content_type": "audio/webm",
            "expected_bytes": 27_000_000,
            "duration_seconds": 10,
        },
        headers={"Idempotency-Key": uid()},
    )
    assert r.status in (413, 422), r.status
    assert r.json()["code"] == "upload_too_large"
    assert "26214400" in r.json()["message"]


@pytest.mark.ci_safe
def test_microphone_denied_falls_back_to_typing(page, stack, probe):
    page.add_init_script(
        "navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Permission denied','NotAllowedError'))"
    )
    enter(page, stack, "capture")
    page.get_by_role("button", name="Record a thought").click()
    expect(page.get_by_text("Microphone access was blocked")).to_be_visible()
    expect(page.get_by_text("typed capture works", exact=False)).to_be_visible()
    send(page, f"typed fallback {uid()}")
    expect(page.get_by_text(re.compile(r"\d+ found"))).to_be_visible(timeout=15000)


# --- accessibility / metadata known issues ---------------------------------


@pytest.mark.known_issue(owner="B", note="sidebar/header icon buttons lack an accessible name")
def test_all_buttons_have_accessible_names(page, stack, probe):
    enter(page, stack, "today")
    page.wait_for_load_state("networkidle")
    nameless = page.evaluate(
        """() => [...document.querySelectorAll('button,a[href]')].filter(e => e.offsetParent !== null)
            .filter(e => !(e.getAttribute('aria-label') || e.innerText || e.getAttribute('title') || '').trim())
            .map(e => e.outerHTML.slice(0, 80))"""
    )
    assert not nameless, nameless


@pytest.mark.known_issue(owner="B", note="tab title is always 'Metis'")
def test_tab_title_reflects_surface(page, stack, probe):
    enter(page, stack, "tasks")
    page.wait_for_timeout(300)
    assert page.title() != "Metis", page.title()


SURFACES = ["today", "capture", "inbox", "tasks", "projects", "search", "alerts", "settings"]


@pytest.mark.known_issue(
    owner="B",
    note="NEW: color-contrast 1.59 on the FIXTURE MODE label (#ecb766 on #f0f0f0) and landing page muted text",
)
def test_axe_no_serious_violations_on_any_surface(page, stack, probe):
    from axe_playwright_python.sync_playwright import Axe

    enter(page, stack, "today")
    bad = []
    for route in SURFACES:
        page.goto(f"{stack.web}/#/{route}")
        page.wait_for_timeout(900)  # let enter animations finish before measuring contrast
        bad += [
            (route, v["id"])
            for v in Axe().run(page).response["violations"]
            if v["impact"] in ("serious", "critical")
        ]
    assert not bad, bad
