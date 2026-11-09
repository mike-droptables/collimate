# SPDX-License-Identifier: MIT
"""The run's Git account (``_env_shared.git_account``): the optional ``git``
role read through ``collimate.git_hosts.account_from_record`` (whose rules
test_git_hosts.py holds), and release lookups."""
from __future__ import annotations

import pytest

from collimate import git_hosts
from _env_shared import git_account
from tests._git_fixtures import bind_account, git_account_snapshot


def test_unbound_is_none(monkeypatch):
    bind_account(monkeypatch, None)
    assert git_account.git_account() is None


def test_the_bound_role_is_read_through_account_from_record(monkeypatch):
    for snapshot in (git_account_snapshot(), git_account_snapshot("git", "basic"),
                     git_account_snapshot("gitlab", "ssh_key")):
        bind_account(monkeypatch, snapshot)
        assert git_account.git_account() == git_hosts.account_from_record(snapshot)
    bind_account(monkeypatch, git_account_snapshot(secrets={}))
    with pytest.raises(ValueError) as refused:
        git_account.git_account()
    assert str(refused.value) == git_account.G3.format(what="token")
    assert git_account.GitAccount is git_hosts.GitAccount and git_account.RemoteRefused is git_hosts.RemoteRefused


def test_release_token_only_for_a_github_com_token(monkeypatch):
    lines: list[str] = []
    bind_account(monkeypatch, git_account_snapshot())
    assert git_account.release_token(lines.append) == "ghp_fake" and lines == []
    bind_account(monkeypatch, None)
    assert git_account.release_token(lines.append) is None and lines == []
    for service, auth, over in (("github", "api_key", {"endpoint": "https://ghe.example.com"}),
                                ("gitlab", "api_key", {}), ("github", "ssh_key", {}), ("git", "basic", {})):
        lines.clear()
        bind_account(monkeypatch, git_account_snapshot(service, auth, **over))
        assert git_account.release_token(lines.append) is None
        assert lines == ["[upstream] GitHub release lookups are anonymous: the Git account is not a github.com token"]


def test_release_token_never_raises_and_logs_a_broken_entry(monkeypatch):
    lines: list[str] = []
    bind_account(monkeypatch, git_account_snapshot(secrets={}))
    assert git_account.release_token(lines.append) is None
    assert lines == [f"[upstream] GitHub release lookups are anonymous: {git_account.G3.format(what='token')}"]
    assert git_account.release_token() is None
