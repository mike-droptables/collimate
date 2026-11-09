# SPDX-License-Identifier: MIT
"""The host-free account bridge enforces the host credential contract."""
from __future__ import annotations

from pathlib import Path

import pytest

from collimate import native
from collimate.testing import make_test_bridge

_REPO = Path(__file__).resolve().parents[1]

# A host run's grant names every axis; these name the ones the rules read,
# and the rest floor to the lowest level.


@pytest.fixture
def host_free(tmp_path):
    """``host_free(**fixtures)`` installs a host-free run built from the
    testing harness's fixtures (``make_test_bridge``) and returns its
    bridge."""
    made: list = []

    def install(**fixtures):
        bridge = make_test_bridge(tmp_path, **fixtures)
        made.append(bridge)
        native.set_bridge(bridge)
        return bridge

    try:
        yield install
    finally:
        native._clear_bridge()
        for bridge in made:
            bridge.close()


def _model_account() -> dict:
    return {"id": "acct", "kind": "model", "revision": 1, "secret_fields": ["api_key"],
            "secrets": {"api_key": "one", "expires_at": "2026-01-01T00:00:00Z"}}


@pytest.mark.parametrize("secrets, reason", [
    ({"api_key": None}, "cannot remove saved credentials: api_key"),
    ({"api_key": ""}, "nonempty string, not: api_key"),
    ({"api_key": 7}, "nonempty string, not: api_key"),
    ({"client_secret": "planted"}, "already stores, not: client_secret"),
])
def test_commit_account_cannot_add_or_remove_a_credential__F0414(host_free, secrets, reason):
    """One run cannot erase or plant a credential every other run of the
    account relies on: a removal, a malformed value or a new field refuses
    the whole commit and changes nothing."""
    host_free(accounts={"model": _model_account()})
    with pytest.raises(RuntimeError, match=reason):
        native.commit_account("model", expected_revision=1, secrets=secrets)
    assert native.account("model") == _model_account()


def test_commit_account_replaces_stored_credentials__F0414(host_free):
    """A stored credential and a non-custom account's token bookkeeping are
    replaced, and the commit merges: what it does not name stays."""
    host_free(accounts={"model": _model_account()})
    out = native.commit_account("model", expected_revision=1,
                                secrets={"api_key": "two", "token_type": "Bearer"})
    assert out == {"committed": True, "revision": 2}
    assert native.account("model")["secrets"] == {
        "api_key": "two", "expires_at": "2026-01-01T00:00:00Z", "token_type": "Bearer"}


def test_a_custom_account_commits_only_its_stored_fields__F0414(host_free):
    host_free(accounts={"svc": {"id": "c", "kind": "custom", "revision": 1,
                                "secret_fields": ["token"], "secrets": {"token": "a"}}})
    with pytest.raises(RuntimeError, match="already stores, not: expires_at"):
        native.commit_account("svc", expected_revision=1,
                              secrets={"token": "b", "expires_at": "2026-01-01T00:00:00Z"})
    assert native.commit_account("svc", expected_revision=1, secrets={"token": "b"})["committed"]
