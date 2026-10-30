# SPDX-License-Identifier: MIT
"""The run's Git account (``_env_shared.git_account``): the optional ``git``
role read into a ``GitAccount`` per service and method, the refusals that
name the Key manager fix, which addresses the credential may serve, compare
links, release lookups and the secret's spellings."""
from __future__ import annotations

import base64
import urllib.parse
from functools import partial

import pytest

from collimate import git_hosts
from _env_shared import git_account, git_run
from tests._git_fixtures import bind_account, git_account_snapshot, ssh_key

GH_KNOWN = "box.example.org ssh-ed25519 " + git_hosts.PINNED_HOST_KEYS["github.com"][0].split()[2]


def account(monkeypatch, service="github", auth="api_key", **over) -> git_account.GitAccount:
    bind_account(monkeypatch, git_account_snapshot(service, auth, **over))
    result = git_account.git_account()
    assert result is not None
    return result


def remote(url: str, account_=None) -> git_run.Remote:
    return git_run.resolve_remote(url, account_)


def test_unbound_is_none(monkeypatch):
    bind_account(monkeypatch, None)
    assert git_account.git_account() is None


@pytest.mark.parametrize("service, auth, over, message", [
    ("github", "api_key", {"kind": "github"}, git_account.G1),
    ("github", "api_key", {"kind": "account"}, git_account.G1),
    ("openai", "api_key", {}, git_account.G1),
    ("github", "basic", {}, git_account.G1),
    ("git", "api_key", {}, git_account.G1),
    ("gitlab", "none", {}, git_account.G1),
    ("github", "api_key", {"metadata": {"author_name": None}}, git_account.G2),
    ("github", "api_key", {"metadata": {"author_email": "not-an-email"}}, git_account.G2),
    ("github", "api_key", {"metadata": {"author_name": "A <b>"}}, git_account.G2),
    ("github", "api_key", {"secrets": {}}, git_account.G3.format(what="token")),
    ("gitlab", "api_key", {"secrets": {"api_key": ""}}, git_account.G3.format(what="token")),
    ("git", "basic", {"metadata": {"username": None}}, git_account.G3.format(what="username and token")),
    ("git", "basic", {"secrets": {}}, git_account.G3.format(what="username and token")),
    ("git", "ssh_key", {"secrets": {}}, git_account.G3.format(what="SSH private key")),
    ("github", "api_key", {"endpoint": "https://api.github.com"}, "fix it in Key manager"),
    ("gitlab", "api_key", {"endpoint": "http://gitlab.example.com"}, "must be an https URL"),
    ("git", "basic", {"endpoint": ""}, git_account.G4.format(problem="no server URL")),
    ("git", "ssh_key", {"endpoint": "https://git.example.org"}, "has no server URL"),
    ("git", "ssh_key", {"secrets": {"private_key": partial(ssh_key, "ed25519_encrypted")}}, "protected by a passphrase"),
    ("git", "ssh_key", {"secrets": {"private_key": partial(ssh_key, "ed25519.pub")}}, "That is a public key"),
    ("git", "ssh_key", {"metadata": {"known_hosts": "|1|abc= ssh-ed25519 AAAA"}}, "Known hosts cannot be read"),
])
def test_refusals_g1_to_g6(monkeypatch, service, auth, over, message):
    if callable(over.get("secrets", {}).get("private_key")):  # a throwaway key, made when the test runs
        over = {**over, "secrets": {"private_key": over["secrets"]["private_key"]()}}
    bind_account(monkeypatch, git_account_snapshot(service, auth, **over))
    with pytest.raises(ValueError) as refused:
        git_account.git_account()
    assert message in str(refused.value)
    if "address" in message or "Key manager" in message:
        assert "Key manager" in str(refused.value)


def test_each_service_and_auth_builds_its_account(monkeypatch):
    gh = account(monkeypatch)
    assert (gh.service, gh.auth, gh.web, gh.username, gh.secret) == (
        "github", "api_key", "https://github.com", "x-access-token", "ghp_fake")
    assert (gh.transport, gh.forge, gh.scope(), gh.known_hosts) == ("https", True, "https://github.com/", ())
    assert gh.identity == git_run.Identity("Ada Lovelace", "ada@example.org")
    ghe = account(monkeypatch, endpoint="https://GHE.example.com:443/")
    assert (ghe.web, ghe.scope()) == ("https://ghe.example.com", "https://ghe.example.com/")
    gl = account(monkeypatch, "gitlab", endpoint="https://example.com/gitlab")
    assert (gl.web, gl.username, gl.secret, gl.scope()) == (
        "https://example.com/gitlab", "oauth2", "glpat-fake", "https://example.com/gitlab/")
    basic = account(monkeypatch, "git", "basic")
    assert (basic.web, basic.username, basic.secret, basic.forge) == ("https://git.example.org", "ada", "pw-fake", False)
    none = account(monkeypatch, "git", "none")
    assert (none.web, none.username, none.secret, none.scope(), none.authorization()) == ("", "", "", "", None)
    ssh = account(monkeypatch, "git", "ssh_key", metadata={"known_hosts": GH_KNOWN})
    assert (ssh.transport, ssh.web, ssh.scope(), ssh.authorization()) == ("ssh", "", "", None)
    assert ssh.secret == git_hosts.normalise_private_key(ssh_key())
    assert ssh.known_hosts == git_hosts.pinned_known_hosts() + git_hosts.parse_known_hosts(GH_KNOWN)
    forge_ssh = account(monkeypatch, "gitlab", "ssh_key")
    assert forge_ssh.known_hosts == git_hosts.pinned_known_hosts()


def test_repr_never_carries_the_secret(monkeypatch):
    for service, auth in (("github", "api_key"), ("git", "basic"), ("git", "ssh_key")):
        acct = account(monkeypatch, service, auth)
        for form in acct.secret_forms():
            assert form not in repr(acct) and form not in str(acct)
        assert repr(acct).startswith(f"GitAccount({service}, {auth}, ")
    auth = git_run.HttpsAuth("https://h/", "c2VjcmV0")
    assert "c2VjcmV0" not in repr(auth)
    assert "PRIVATE" not in repr(git_run.SshAuth(ssh_key(), ("h ssh-ed25519 AAAA",)))


@pytest.mark.parametrize("service, auth, over, url, code", [
    ("github", "api_key", {}, "https://gitlab.com/g/p.git", "scope"),
    ("gitlab", "api_key", {"endpoint": "https://example.com/gitlab"}, "https://example.com/other/x.git", "scope"),
    ("gitlab", "api_key", {"endpoint": "https://example.com/gitlab"}, "https://example.com/gitlabx/x.git", "scope"),
    ("github", "api_key", {}, "git@github.com:octo/api.git", "transport"),
    ("git", "basic", {}, "https://other.example.org/x.git", "scope"),
    ("git", "basic", {}, "ssh://git@git.example.org/x.git", "transport"),
    ("git", "none", {}, "git@box.example.org:x.git", "transport"),
    ("git", "ssh_key", {}, "https://git.example.org/x.git", "transport"),
    ("git", "ssh_key", {}, "ssh://git@box.example.org:2222/srv/app.git", "no_host_key"),
    ("github", "ssh_key", {}, "ssh://git@github.com:2222/octo/api.git", "scope"),
])
def test_auth_for_refuses_scope_transport_and_unknown_host(monkeypatch, service, auth, over, url, code):
    acct = account(monkeypatch, service, auth, **over)
    with pytest.raises(git_account.RemoteRefused) as refused:
        acct.auth_for(remote(url))
    assert refused.value.code == code
    if code == "transport":
        assert set(refused.value.fields) == {"method", "scheme", "other"}
    if code == "no_host_key":
        assert refused.value.fields == {"hostport": "[box.example.org]:2222"}
    if code == "scope" and service == "gitlab":
        assert refused.value.fields == {"base": "example.com/gitlab", "host": "example.com"}


def test_auth_for_returns_the_scoped_header_or_ssh_material(monkeypatch):
    gh = account(monkeypatch)
    https = gh.auth_for(remote("https://github.com/octo/api.git"))
    assert https == git_run.HttpsAuth("https://github.com/", git_run.basic_authorization("x-access-token", "ghp_fake"))
    assert base64.b64decode(https.authorization).decode() == "x-access-token:ghp_fake"
    gl = account(monkeypatch, "gitlab", endpoint="https://example.com/gitlab")
    assert gl.auth_for(remote("https://example.com/gitlab/g/s/p.git")).scope == "https://example.com/gitlab/"
    assert account(monkeypatch, "git", "none").auth_for(remote("https://anywhere.example/x")) is None
    ssh = account(monkeypatch, "git", "ssh_key",
                  metadata={"known_hosts": "[box.example.org]:2222 " + GH_KNOWN.split(" ", 1)[1]})
    material = ssh.auth_for(remote("ssh://git@box.example.org:2222/srv/app.git"))
    assert isinstance(material, git_run.SshAuth)
    assert material.private_key == git_hosts.normalise_private_key(ssh_key())
    assert material.known_hosts[-1].startswith("[box.example.org]:2222 ssh-ed25519 ")
    assert all(k.line() in material.known_hosts for k in git_hosts.pinned_known_hosts())
    forge = account(monkeypatch, "github", "ssh_key")
    forge_remote = remote("https://github.com/octo/api.git", forge)
    assert forge_remote == git_run.Remote("ssh://git@github.com/octo/api.git", ("ssh",), "github.com")
    assert isinstance(forge.auth_for(forge_remote), git_run.SshAuth)


def test_compare_url_per_service(monkeypatch):
    gh = account(monkeypatch)
    assert (gh.compare_url("https://github.com/octo/api.git", "main", "feat/x y")
            == "https://github.com/octo/api/compare/main...feat/x%20y?expand=1")
    gl = account(monkeypatch, "gitlab", endpoint="https://example.com/gitlab")
    assert (gl.compare_url("https://example.com/gitlab/g/s/app.git", "main", "fix")
            == "https://example.com/gitlab/g/s/app/-/compare/main...fix")
    assert account(monkeypatch, "git", "basic").compare_url("https://git.example.org/x.git", "a", "b") is None


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


def test_secret_forms_per_auth(monkeypatch):
    token = "ghp_a/b+c"
    gh = account(monkeypatch, secrets={"api_key": token})
    assert gh.secret_forms() == (token, urllib.parse.quote(token, safe=""),
                                 git_run.basic_authorization("x-access-token", token), f"x-access-token:{token}")
    basic = account(monkeypatch, "git", "basic", metadata={"username": "a b"}, secrets={"password": "p@ss"})
    assert basic.secret_forms() == ("p@ss", "p%40ss", git_run.basic_authorization("a b", "p@ss"), "a%20b:p%40ss")
    ssh = account(monkeypatch, "git", "ssh_key")
    forms = ssh.secret_forms()
    normalised = git_hosts.normalise_private_key(ssh_key())
    body = [line for line in normalised.splitlines() if not line.startswith("-----")]
    assert forms[:4] == (normalised, normalised.rstrip("\n"), normalised.replace("\n", "\r\n"), "".join(body))
    assert set(forms[4:]) == {line for line in body if len(line) >= 16}
    assert account(monkeypatch, "git", "none").secret_forms() == ()
