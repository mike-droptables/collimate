"""The canonical run-log formatter against the shared golden vectors.

``process_manager.log_format.format_log_line`` renders a ``run_events`` row
the way the frontend's ``runLogFormat.ts`` does, byte for byte: the live
drawer and the persisted ``logs.txt`` must hold the same lines. The two
implementations stay; ``fixtures/run-log-vectors.json`` is the one statement
of the contract (the tag map, the ``[LEVEL]``-stderr re-tagging, the
STDERR-not-ERR rule, the allow-list drops, the UTC-to-local timestamps, the
RUN bookends, the null and malformed payloads), and both suites read it
(``frontend/src/lib/runLogFormat.test.ts`` is the other).

Timestamps render in LOCAL time, so the vectors fix the zone: the module
sets ``TZ`` to the fixture's ``timezone`` for its tests and restores it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from process_manager.log_format import format_log_line

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "run-log-vectors.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True, scope="module")
def _vector_time_zone():
    previous = os.environ.get("TZ")
    os.environ["TZ"] = VECTORS["timezone"]
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


@pytest.mark.parametrize("row", VECTORS["cases"], ids=[row["name"] for row in VECTORS["cases"]])
def test_run_log_line_matches_the_golden_vector(row):
    assert format_log_line(row["event_type"], row["payload"], row["created_at"]) == row["line"]
