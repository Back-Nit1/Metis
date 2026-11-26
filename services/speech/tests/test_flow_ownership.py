from concurrent.futures import ThreadPoolExecutor

import pytest
from metis_contracts import FlowSettings, MetisError
from metis_speech.flow import Sessions


def test_other_owner_cannot_read_append_finish_or_cancel():
    sessions = Sessions(300)
    session = sessions.create(None, None, FlowSettings(), owner="alice")
    for operation in (
        lambda: sessions.append(session.id, b"\0\0", owner="bob"),
        lambda: sessions.begin_finish(session.id, owner="bob"),
        lambda: sessions.end(session.id, owner="bob"),
    ):
        with pytest.raises(MetisError):
            operation()
    assert sessions.append(session.id, b"\0\0", owner="alice")[0].seconds > 0


def test_concurrent_admission_cannot_replace_active_session():
    sessions = Sessions(300)

    def create(owner):
        try:
            return sessions.create(None, None, FlowSettings(), owner=owner)
        except MetisError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, [str(i) for i in range(8)]))
    assert len([s for s in results if s is not None]) == 1


def test_expired_session_releases_admission(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("metis_speech.flow.time.monotonic", lambda: now[0])
    sessions = Sessions(300)
    first = sessions.create(None, None, FlowSettings(), owner="alice")
    # Dataclass's default factory retains original monotonic; append sets the test clock.
    sessions.append(first.id, b"\0\0", owner="alice")
    now[0] = 10000
    second = sessions.create(None, None, FlowSettings(), owner="bob")
    assert first.id != second.id
    with pytest.raises(MetisError):
        sessions.begin_finish(first.id, owner="alice")
