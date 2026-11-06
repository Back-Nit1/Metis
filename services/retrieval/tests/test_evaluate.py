"""The eval harness runs end to end and the safety metrics hold in every arm.

Quality scores are not asserted: the fixture embedder is not a real model.
"""

from __future__ import annotations

import json

from metis_contracts import reset_settings_cache
from metis_retrieval import FixtureEmbedder
from metis_retrieval.evaluate import FIXTURE, HARD_METRICS, MODES, run, summarize


def test_fixture_is_labelled_consistently():
    data = json.loads(FIXTURE.read_text())
    ids = {m["id"] for m in data["memories"]}
    assert len(data["queries"]) == 40
    assert {q["split"] for q in data["queries"]} == {"dev", "heldout"}
    for q in data["queries"]:
        for key in ("expect", "forbid", "stale"):
            assert set(q.get(key, [])) <= ids, (q["id"], key)


def test_no_leaks_stale_or_broken_links_in_any_mode():
    reset_settings_cache()
    summary = summarize(run(json.loads(FIXTURE.read_text()), FixtureEmbedder()))
    assert set(summary) == set(MODES)
    for mode, metrics in summary.items():
        for key in HARD_METRICS:
            assert metrics[key] == 0, f"{mode}.{key}"
