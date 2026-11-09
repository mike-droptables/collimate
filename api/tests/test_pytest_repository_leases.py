"""The per-cell repository lease registry (``api/collimate_server/repository_leases.py``):
claim, holder, release and the wait on a releasing entry."""
from __future__ import annotations

import asyncio
import importlib
import sys
import threading

import pytest


@pytest.fixture
def leases():
    """A fresh registry: the module is imported anew for each test."""
    sys.modules.pop("collimate_server.repository_leases", None)
    module = importlib.import_module("collimate_server.repository_leases")
    yield module
    sys.modules.pop("collimate_server.repository_leases", None)


def test_a_claim_makes_the_run_the_holder(leases):
    lease = leases.claim("ch", "cell", "run-aaaaaaaa1", "Agent")
    assert leases.holder("ch", "cell") is lease
    assert (lease.run_id, lease.label, lease.state, lease.materialized, lease.seq) == \
        ("run-aaaaaaaa1", "Agent", "live", {}, 0)
    assert not lease.released.is_set()
    assert leases.holder("ch", "other") is None and leases.holder("other", "cell") is None
    assert leases.claim("ch", "cell", "run-aaaaaaaa1", "Agent") is lease


def test_another_run_is_busy_shaped_like_surface_taken(leases):
    leases.claim("ch", "cell", "0123456789abcdef", "Clone")
    with pytest.raises(leases.RepositoryBusy) as busy:
        leases.claim("ch", "cell", "fedcba9876543210", "Terminal")
    body = busy.value.response()
    assert body == {
        "error": "repository_busy", "owner": "Clone", "run_id": "0123456789abcdef",
        "message": "git is in use by run 01234567 on this cell; Duplicate the cell for a second working copy",
    }
    from collimate_server.served_surfaces import SurfaceTaken
    assert {"error", "owner", "run_id", "message"} <= set(SurfaceTaken("o", "r", "p").response())
    assert leases.holder("ch", "cell").run_id == "0123456789abcdef"


def test_release_goes_through_releasing_and_sets_the_event(leases):
    lease = leases.claim("ch", "cell", "run1", "Agent")
    lease.materialized[""] = "a" * 64
    assert leases.begin_release("ch", "cell", "run2") is None
    assert leases.begin_release("ch", "cell", "run1") is lease and lease.state == "releasing"
    with pytest.raises(leases.RepositoryBusy):
        leases.claim("ch", "cell", "run2", "Agent")             # releasing still holds
    other = leases.claim("ch", "cell2", "run1", "Agent")
    assert [id(gone) for gone in leases.release_run("run1")] == [id(lease), id(other)]
    assert lease.released.is_set() and other.released.is_set()
    assert leases.holder("ch", "cell") is None and leases.release_run("run1") == []
    assert leases.claim("ch", "cell", "run2", "Agent").run_id == "run2"


def test_recovery_claims_straight_into_releasing(leases):
    lease = leases.claim("ch", "cell", "crashed", "recovery", state="releasing")
    assert lease.state == "releasing"
    with pytest.raises(leases.RepositoryBusy):
        leases.claim("ch", "cell", "new-run", "Agent")


def test_each_harvest_attempt_takes_a_newer_seq(leases):
    lease = leases.claim("ch", "cell", "run1", "Agent")
    assert [lease.next_seq() for _ in range(3)] == [1, 2, 3] and lease.seq == 3


def test_wait_released_returns_at_once_without_a_releasing_lease(leases):
    assert asyncio.run(leases.wait_released("ch", "cell", timeout=0.01))
    leases.claim("ch", "cell", "run1", "Agent")
    assert asyncio.run(leases.wait_released("ch", "cell", timeout=0.01))     # a live lease is not waited on


def test_wait_released_waits_for_a_release_from_another_thread(leases):
    leases.claim("ch", "cell", "run1", "Agent")
    leases.begin_release("ch", "cell", "run1")
    timer = threading.Timer(0.2, leases.release_run, args=("run1",))
    timer.start()
    try:
        assert asyncio.run(leases.wait_released("ch", "cell", timeout=5))
    finally:
        timer.cancel()
    assert leases.holder("ch", "cell") is None


def test_wait_released_gives_up_after_its_timeout(leases, caplog):
    leases.claim("ch", "cell", "run1-long-id", "Agent")
    leases.begin_release("ch", "cell", "run1-long-id")
    with caplog.at_level("WARNING", logger="repository_leases"):
        assert not asyncio.run(leases.wait_released("ch", "cell", timeout=0.1))
    assert "run1-lon" in caplog.text
    assert leases.holder("ch", "cell").state == "releasing"
