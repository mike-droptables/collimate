# SPDX-License-Identifier: MIT
"""The run's Git account: the optional ``git`` role, a Key manager GitHub,
GitLab or Git server entry (kind git) with its commit author. Every Git
consumer (the Git engine, the Agent's harness, Launch, release lookups) reads
it here, so one rule decides which host may receive the credential: no role
bound is anonymous HTTPS, and a bound entry that cannot serve is refused,
never silently ignored. The entry's rules, :class:`GitAccount` and the
G1–G6 refusals are ``collimate.git_hosts``' (``account_from_record``),
shared with the host, which reads the same entry into the run's
environment."""
from __future__ import annotations

from typing import Callable

from collimate.git_hosts import (
    G1, G2, G3, G4, G5, G6, GitAccount, RemoteRefused, account_from_record,
)

__all__ = ["G1", "G2", "G3", "G4", "G5", "G6", "ROLE", "GitAccount", "RemoteRefused", "git_account",
           "release_token"]

ROLE = "git"

_NO_ANONYMOUS = "[upstream] GitHub release lookups are anonymous: {why}"
_NOT_GITHUB_COM = "the Git account is not a github.com token"


def git_account(role: str = ROLE) -> "GitAccount | None":
    """The bound Git account; None when the run binds none. Raises
    ``ValueError`` with the fix (G1–G6) when the bound entry cannot serve.
    Never logs a snapshot field."""
    import gen

    try:
        account = gen.account(role)
    except LookupError:
        return None
    return account_from_record(account)


def release_token(log: "Callable[[str], None] | None" = None) -> "str | None":
    """The token for GitHub release lookups: the Git account's secret only
    when it is a github.com token; anonymous (logged through ``log``)
    otherwise, never failing the run. Never raises."""
    try:
        account = git_account()
    except ValueError as exc:
        if log is not None:
            log(_NO_ANONYMOUS.format(why=exc))
        return None
    if account is None:
        return None
    if account.service == "github" and account.auth == "api_key" and account.web == "https://github.com":
        return account.secret
    if log is not None:
        log(_NO_ANONYMOUS.format(why=_NOT_GITHUB_COM))
    return None
