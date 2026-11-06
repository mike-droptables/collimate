# SPDX-License-Identifier: MIT
"""The hardened git process layer (``_env_shared.git_run``): no ambient
config, exactly one transport, the credential only where the account allows
it (the Basic header the host put in the run's environment, scoped to the
account's base, or an SSH key in a 0600 file for one git command), the
account environment forwarded to children that do not inherit it, and git's
own words reaching the log only scrubbed, never kept."""
from __future__ import annotations

import asyncio
import base64
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from collimate import git_hosts
from _env_shared import git_account, git_run
from tests._git_fixtures import GIT_OK, HTTP_TOKEN, bind_account, carry_account, git_account_snapshot, ssh_key

TOKEN = "ghp_planted_s3cret"
GH = git_run.Remote("https://github.com/octo/api.git", ("https",))
AUTH = git_run.HttpsAuth("https://github.com/", git_run.basic_authorization("x-access-token", TOKEN))
KEY = git_hosts.normalise_private_key(ssh_key())
BOX_LINE = "[box.example.org]:2222 " + git_hosts.PINNED_HOST_KEYS["github.com"][0].split(" ", 1)[1]
BOX = git_run.Remote("ssh://git@box.example.org:2222/srv/app.git", ("ssh",), "[box.example.org]:2222")
SSH_AUTH = git_run.SshAuth(KEY, (BOX_LINE,))


def _fake_git(tmp_path: Path, body: str) -> Path:
    """A directory holding an executable ``git`` shell script."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "git"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _bound(monkeypatch, service="github", auth="api_key", **over) -> git_account.GitAccount:
    bind_account(monkeypatch, git_account_snapshot(service, auth, **over))
    return git_account.git_account()


def test_resolve_remote_per_address_and_account(monkeypatch):
    monkeypatch.setenv("GIT_REMOTE_OVERRIDE", "file:///tmp/x")
    assert git_run.resolve_remote(GH.url, None) == GH
    gh_ssh = _bound(monkeypatch, "github", "ssh_key")
    assert git_run.resolve_remote(GH.url, gh_ssh) == git_run.Remote(
        "ssh://git@github.com/octo/api.git", ("ssh",), "github.com")
    gl_ssh = _bound(monkeypatch, "gitlab", "ssh_key", endpoint="https://example.com/gitlab",
                    metadata={"known_hosts": BOX_LINE.replace("[box.example.org]:2222", "example.com")})
    assert git_run.resolve_remote("https://example.com/gitlab/group/sub/app.git", gl_ssh) == git_run.Remote(
        "ssh://git@example.com/group/sub/app.git", ("ssh",), "example.com")
    # An address outside the forge's base stays https (auth_for then refuses it).
    assert git_run.resolve_remote("https://example.com/other/x.git", gl_ssh).protocols == ("https",)
    assert git_run.resolve_remote(BOX.url, None) == BOX
    assert git_run.resolve_remote("git@box.example.org:team/r.git", None) == git_run.Remote(
        "git@box.example.org:team/r.git", ("ssh",), "box.example.org")
    assert git_run.resolve_remote("git@box.example.org:~/r.git", gh_ssh).url == "git@box.example.org:~/r.git"
    token = _bound(monkeypatch)
    assert git_run.resolve_remote(GH.url, token) == GH


def test_transport_is_the_identity_in_production():
    assert git_run.transport(GH, GH.url) is GH


def test_prefix_allows_exactly_one_transport_and_ssl_verify():
    prefix = git_run.git_prefix(GH)
    configs = [prefix[i + 1] for i, word in enumerate(prefix) if word == "-c"]
    assert "protocol.allow=never" in configs and "http.sslVerify=true" in configs
    assert configs == [*(git_hosts.HARDENING_FLAGS[1::2]), "http.followRedirects=false", "http.sslVerify=true",
                       "protocol.allow=never", "protocol.https.allow=always", "http.lowSpeedLimit=1",
                       "http.lowSpeedTime=30"]
    # A repository's own settings are its own: nothing overrides what a cell keeps.
    for kept in ("core.autocrlf", "core.quotePath", "init.defaultBranch", "merge.renames", "core.fileMode",
                 "advice.detachedHead"):
        assert not any(c.lower().startswith(kept.lower() + "=") for c in configs), kept
    assert [c for c in configs if c.startswith("protocol.") and c.endswith("=always")] == ["protocol.https.allow=always"]
    for setting in ("credential.helper=", "core.askPass=", f"core.hooksPath={os.devnull}",
                    "http.followRedirects=false", "core.fsmonitor=false", "submodule.recurse=false",
                    "commit.gpgSign=false"):
        assert setting in configs
    assert "ssh.variant=ssh" not in configs
    assert prefix.index("protocol.allow=never") < prefix.index("protocol.https.allow=always")
    ssh_configs = git_run.git_prefix(BOX)
    assert [c for c in ssh_configs if c.startswith("protocol.") and c.endswith("=always")] == ["protocol.ssh.allow=always"]
    assert "ssh.variant=ssh" in ssh_configs
    file_prefix = git_run.git_prefix(git_run.Remote("file:///x.git", ("file",)))
    assert "protocol.file.allow=always" in file_prefix and "protocol.https.allow=always" not in file_prefix


@pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")
def test_production_policy_refuses_local_and_file_remotes(tmp_path):
    bare = tmp_path / "local.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(bare)], check=True, capture_output=True)
    git = git_run.Git(GH, None, tmp_path)
    for url in (str(bare), f"file://{bare}"):
        with pytest.raises(git_run.GitFailure) as failure:
            asyncio.run(git.run(["ls-remote", url], tmp_path, network=True))
        assert failure.value.code != 0
    # The same repository is readable when its remote allows the file transport.
    allowed = git_run.Git(git_run.Remote(f"file://{bare}", ("file",)), None, tmp_path)
    assert asyncio.run(allowed.run(["ls-remote", f"file://{bare}"], tmp_path, network=True)).code == 0


def test_environment_scrubs_ambient_git_variables_askpass_and_ssh(tmp_path, monkeypatch):
    ambient = {"GIT_TRACE": "1", "GIT_CONFIG_KEY_17": "credential.helper", "GIT_ASKPASS": "/x",
               "GIT_SSH_COMMAND": "ssh -v", "GIT_SSH": "/s", "GIT_ADVICE": "1", "HTTPS_PROXY": "http://proxy:3128",
               **{name: "planted" for name in ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "SSH_AUTH_SOCK")}}
    for key, value in ambient.items():
        monkeypatch.setenv(key, value)
    for auth in (None, AUTH, SSH_AUTH):
        env = git_run.git_environment(auth, tmp_path)
        assert not {"GIT_TRACE", "GIT_CONFIG_KEY_17", "GIT_ASKPASS", "GIT_SSH_COMMAND", "GIT_SSH", "GIT_ADVICE",
                    "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "SSH_AUTH_SOCK"} & env.keys()
        assert "planted" not in env.values()
        assert env["HTTPS_PROXY"] == "http://proxy:3128"
        assert env["HOME"] == env["XDG_CONFIG_HOME"] == str(tmp_path)
        assert (env["GIT_CONFIG_NOSYSTEM"], env["GIT_CONFIG_GLOBAL"], env["GIT_TERMINAL_PROMPT"]) == ("1", os.devnull, "0")
        assert (env["LC_ALL"], env["LANGUAGE"]) == ("C", "")


def test_the_runs_account_block_author_and_forge_names_reach_git(tmp_path, monkeypatch):
    """What the host put in the run's environment for its Git account
    survives the scrub: the scoped header (once, with ``auth``'s own), the
    author, the forge names. ``identity`` replaces the author."""
    gh = git_hosts.account_from_record(git_account_snapshot("github", "api_key", secrets={"api_key": TOKEN}))
    carry_account(monkeypatch, gh)
    for auth in (None, AUTH):
        env = git_run.git_environment(auth, tmp_path)
        assert {k: env[k] for k in env if k.startswith("GIT_CONFIG_") and k[11:12] in "CKV"} == {
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {AUTH.authorization}"}
        assert (env["GIT_AUTHOR_NAME"], env["GIT_COMMITTER_EMAIL"]) == ("Ada Lovelace", "ada@example.org")
        assert (env["GITHUB_TOKEN"], env["GH_TOKEN"]) == (TOKEN, TOKEN)
    other = git_run.HttpsAuth("https://gitlab.com/", git_run.basic_authorization("oauth2", "glpat"))
    assert git_hosts.git_config_entries(git_run.git_environment(other, tmp_path)) == [
        ("http.https://github.com/.extraHeader", f"Authorization: Basic {AUTH.authorization}"),
        ("http.https://gitlab.com/.extraHeader", f"Authorization: Basic {other.authorization}")]
    named = git_run.git_environment(None, tmp_path, git_run.Identity("Grace Hopper", "grace@example.com"))
    assert (named["GIT_AUTHOR_NAME"], named["GIT_COMMITTER_EMAIL"]) == ("Grace Hopper", "grace@example.com")


@pytest.mark.parametrize("block", [
    {"GIT_CONFIG_KEY_1": "core.sshCommand", "GIT_CONFIG_VALUE_1": "touch /tmp/x"},
    {"GIT_CONFIG_KEY_1": "http.http://github.com/.extraHeader", "GIT_CONFIG_VALUE_1": "x"},
    {"GIT_CONFIG_KEY_1": "http.https://u@github.com/.extraHeader", "GIT_CONFIG_VALUE_1": "x"},
    {"GIT_CONFIG_KEY_1": "http.https://github.com.extraHeader", "GIT_CONFIG_VALUE_1": "x"},
    {"GIT_CONFIG_KEY_1": "http.https://github.com/.proxy", "GIT_CONFIG_VALUE_1": "http://evil:1"},
    {"GIT_CONFIG_KEY_1": "include.path", "GIT_CONFIG_VALUE_1": "/tmp/x"},
    {"GIT_CONFIG_KEY_1": "http.https://github.com/.extraHeader"},
])
def test_a_block_with_any_other_key_never_reaches_git(tmp_path, monkeypatch, block):
    """The block survives only whole and only when every key is an
    extraHeader under an https base or ``safe.directory``."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.https://github.com/.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"Authorization: Basic {AUTH.authorization}")
    for name, value in block.items():
        monkeypatch.setenv(name, value)
    env = git_run.git_environment(None, tmp_path)
    assert not any(name.startswith(("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY", "GIT_CONFIG_VALUE")) for name in env)
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "*")
    assert git_hosts.git_config_entries(git_run.git_environment(None, tmp_path))[1] == ("safe.directory", "*")


def test_the_runs_format_pins_reach_git(tmp_path, monkeypatch):
    """The ref format and hash the run pins (files, sha1) survive the scrub,
    so a git whose own defaults moved on still makes repositories the
    state gate keeps; nothing sets them when the run does not."""
    assert not {"GIT_DEFAULT_REF_FORMAT", "GIT_DEFAULT_HASH"} & git_run.git_environment(None, tmp_path).keys()
    monkeypatch.setenv("GIT_DEFAULT_REF_FORMAT", "files")
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha1")
    env = git_run.git_environment(None, tmp_path)
    assert (env["GIT_DEFAULT_REF_FORMAT"], env["GIT_DEFAULT_HASH"]) == ("files", "sha1")


def test_the_ceiling_is_the_runs_beside_homes_parent(tmp_path, monkeypatch):
    home = tmp_path / "h" / "collimate-git-1-x"
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)
    assert git_run.git_environment(None, home)["GIT_CEILING_DIRECTORIES"] == str(home.parent)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "/srv/runs")
    assert git_run.git_environment(None, home)["GIT_CEILING_DIRECTORIES"] == f"/srv/runs{os.pathsep}{home.parent}"


def test_forwarding_adds_the_account_environment_back_once(monkeypatch):
    """A child whose environment replaces the run's gets the account's block
    appended to its own, the author and forge names over its own, and in a
    container ``safe.directory=*``; never GIT_CONFIG_GLOBAL or NOSYSTEM."""
    gl = git_hosts.account_from_record(git_account_snapshot("gitlab", endpoint="https://example.com/gitlab"))
    carry_account(monkeypatch, gl)
    header = (f"http.{gl.scope()}.extraHeader", f"Authorization: Basic {gl.authorization()}")
    leg = {"PATH": "/usr/bin", "GIT_AUTHOR_NAME": "Mallory", "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "core.pager", "GIT_CONFIG_VALUE_0": "cat"}
    host = git_run.forward_git_environment(leg)
    assert git_hosts.git_config_entries(host) == [("core.pager", "cat"), header]
    assert (host["GIT_AUTHOR_NAME"], host["GITLAB_TOKEN"], host["GITLAB_HOST"]) == (
        "Ada Lovelace", "glpat-fake", "https://example.com/gitlab")
    container = git_run.forward_git_environment(leg, container=True)
    assert git_hosts.git_config_entries(container) == [("core.pager", "cat"), header, ("safe.directory", "*")]
    assert git_run.forward_git_environment(container, container=True) == container
    for env in (host, container):
        assert not {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"} & env.keys()
    assert leg["GIT_AUTHOR_NAME"] == "Mallory"                 # a new dict
    carry_account(monkeypatch, None)
    assert git_run.forward_git_environment({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}
    assert git_run.forward_git_environment({}, container=True) == {
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}


def test_https_auth_is_one_basic_header_scoped_to_the_account(tmp_path):
    scoped = git_run.HttpsAuth("https://example.com/gitlab/", git_run.basic_authorization("oauth2", TOKEN))
    env = git_run.git_environment(scoped, tmp_path)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://example.com/gitlab/.extraHeader"
    value = env["GIT_CONFIG_VALUE_0"]
    assert value == f"Authorization: Basic {scoped.authorization}"
    assert base64.b64decode(value.removeprefix("Authorization: Basic ")).decode() == f"oauth2:{TOKEN}"
    assert [k for k, v in env.items() if TOKEN in v or scoped.authorization in v] == ["GIT_CONFIG_VALUE_0"]
    assert "GIT_SSH_COMMAND" not in env
    prefix = git_run.git_prefix(GH)
    assert not any(TOKEN in word or scoped.authorization in word for word in prefix)


def test_ssh_command_is_the_hardened_option_set_with_quoted_paths(tmp_path):
    key, known = tmp_path / "a dir" / "key", tmp_path / "a dir" / "known hosts"
    command = git_run.ssh_command("/usr/bin/ssh", key, known)
    words = shlex.split(command)
    assert words[:5] == ["/usr/bin/ssh", "-F", "/dev/null", "-i", str(key)]
    options = [words[i + 1] for i, w in enumerate(words) if w == "-o"]
    assert options == [
        "IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes", "StrictHostKeyChecking=yes",
        f"UserKnownHostsFile={known}", "GlobalKnownHostsFile=/dev/null", "HashKnownHosts=no", "UpdateHostKeys=no",
        "CheckHostIP=no", "VerifyHostKeyDNS=no", "CanonicalizeHostname=no", "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no", "PreferredAuthentications=publickey", "ForwardAgent=no",
        "ForwardX11=no", "ClearAllForwardings=yes", "PermitLocalCommand=no", "ProxyCommand=none",
        "ProxyJump=none", "ControlMaster=no", "ControlPath=none", "ConnectTimeout=30",
        "ServerAliveInterval=15", "ServerAliveCountMax=4", "LogLevel=ERROR"]
    assert len(words) == 5 + 2 * len(options)
    assert "  " not in command and "\n" not in command


def test_ssh_environment_has_no_git_config_and_https_has_no_ssh_command(tmp_path):
    env = git_run.git_environment(SSH_AUTH, tmp_path, ssh_command="/usr/bin/ssh -F /dev/null")
    assert (env["GIT_SSH_COMMAND"], env["GIT_SSH_VARIANT"]) == ("/usr/bin/ssh -F /dev/null", "ssh")
    assert not any(k.startswith("GIT_CONFIG_") and k not in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL") for k in env)
    assert not any(KEY.strip() in v or line in v for v in env.values() for line in KEY.splitlines()[1:-1])
    assert "GIT_SSH_COMMAND" not in git_run.git_environment(SSH_AUTH, tmp_path)
    assert "GIT_SSH_COMMAND" not in git_run.git_environment(AUTH, tmp_path, ssh_command="/usr/bin/ssh")


def test_git_init_guard_refuses_out_of_scope_https_and_unpinned_ssh(tmp_path):
    with pytest.raises(ValueError):
        git_run.Git(git_run.Remote("https://gitlab.com/g/p.git", ("https",)), AUTH, tmp_path)
    with pytest.raises(ValueError):
        git_run.Git(git_run.Remote("https://github.com.evil.example/o/r.git", ("https",)), AUTH, tmp_path)
    with pytest.raises(ValueError):
        git_run.Git(BOX, AUTH, tmp_path)
    with pytest.raises(ValueError):
        git_run.Git(git_run.Remote("ssh://git@other.example/r.git", ("ssh",), "other.example"), SSH_AUTH, tmp_path)
    with pytest.raises(ValueError):
        git_run.Git(GH, SSH_AUTH, tmp_path)
    git_run.Git(GH, AUTH, tmp_path)
    git_run.Git(BOX, SSH_AUTH, tmp_path)
    git_run.Git(git_run.Remote("file:///x.git", ("file",)), AUTH, tmp_path)
    git_run.Git(git_run.Remote("file:///x.git", ("file",)), SSH_AUTH, tmp_path)


def test_missing_ssh_is_missing_ssh(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(_fake_git(tmp_path, "echo 'git version 2.43.0'")))
    git = git_run.Git(BOX, SSH_AUTH, tmp_path)
    with pytest.raises(git_run.GitFailure) as failure:
        asyncio.run(git.run(["ls-remote", BOX.url], tmp_path, network=True))
    assert (failure.value.kind, failure.value.command, failure.value.code) == ("missing_ssh", "ls-remote", -1)
    assert [p.name for p in tmp_path.iterdir()] == ["bin"]


def test_no_lazy_fetch_command_writes_no_key(tmp_path, git_spy):
    seen: list = []
    git_spy.before("merge-base", lambda: seen.append(sorted(p.name for p in tmp_path.iterdir())))
    git = git_run.Git(BOX, SSH_AUTH, tmp_path)
    asyncio.run(git.run(["merge-base", "--is-ancestor", "a", "b"], tmp_path, ok=(0, 1, 128), no_lazy_fetch=True))
    assert seen == [[]]
    argv, env = git_spy.calls[-1]
    assert "GIT_SSH_COMMAND" not in env and env["GIT_NO_LAZY_FETCH"] == "1"


def test_git_never_runs_from_a_relative_or_empty_path_entry(tmp_path, monkeypatch):
    """An empty or relative PATH entry resolves against git's cwd, a checkout
    of the repository: a ``git`` it ships must never run (with the token in
    its environment)."""
    checkout = tmp_path / "checkout"
    planted = tmp_path / "planted"
    for where in (checkout, checkout / "bin"):
        where.mkdir(parents=True, exist_ok=True)
        (where / "git").write_text(f"#!/bin/sh\ntouch {planted}\necho 'git version 2.99.0'\n")
        (where / "git").chmod(0o755)
    real = _fake_git(tmp_path, "echo 'git version 2.43.0'")
    monkeypatch.setenv("PATH", os.pathsep.join(["", "bin", str(real)]))
    assert git_run.git_environment(AUTH, tmp_path)["PATH"] == str(real)
    git = git_run.Git(GH, AUTH, tmp_path)
    assert asyncio.run(git.run(["--version"], checkout)).stdout == b"git version 2.43.0\n"
    monkeypatch.setattr(git_run.tempfile, "tempdir", str(checkout))
    assert asyncio.run(git_run.git_version()) == "2.43.0"
    assert not planted.exists()


@pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")
def test_repository_discovery_never_climbs_above_home(tmp_path):
    """A repository planted above git's HOME (``/tmp/.git``) never supplies
    config: the ceiling stops discovery at HOME."""
    outer = tmp_path / "outer"
    subprocess.run(["git", "init", "--quiet", str(outer)], check=True, capture_output=True)
    home = outer / "collimate-git-x"
    home.mkdir()
    assert git_run.git_environment(None, home)["GIT_CEILING_DIRECTORIES"] == str(outer)
    git = git_run.Git(GH, None, home)
    found = asyncio.run(git.run(["rev-parse", "--git-dir"], home, ok=(0, 128)))
    assert found.code == 128


def test_anonymous_environment_has_no_extra_header(tmp_path):
    env = git_run.git_environment(None, tmp_path)
    assert not any(k.startswith("GIT_CONFIG_") and k not in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL")
                   for k in env)
    assert "GIT_NO_LAZY_FETCH" not in env


def test_identity_sets_author_and_committer_and_none_means_gits_own(tmp_path, monkeypatch):
    env = git_run.git_environment(None, tmp_path, git_run.Identity("Ada Lovelace", "ada@example.org"))
    assert (env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"]) == ("Ada Lovelace", "ada@example.org")
    assert (env["GIT_COMMITTER_NAME"], env["GIT_COMMITTER_EMAIL"]) == ("Ada Lovelace", "ada@example.org")
    carry_account(monkeypatch, None)
    assert not set(git_hosts.AUTHOR_ENV) & git_run.git_environment(None, tmp_path).keys()


@pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")
def test_without_an_account_git_asks_who_you_are(tmp_path, monkeypatch):
    """D8: no account and no identity, so a commit is git's own refusal (its
    words reach the log after the failure line), never a made-up author."""
    carry_account(monkeypatch, None)
    monkeypatch.delenv("EMAIL", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    logged: list = []
    git = git_run.Git(GH, None, home, log=logged.append)
    asyncio.run(git.run(["init", "-q", "-b", "main"], repo))
    (repo / "f.txt").write_text("x\n")
    asyncio.run(git.run(["add", "f.txt"], repo))
    probe = asyncio.run(git.run(["var", "GIT_COMMITTER_IDENT"], repo, ok=(0, 128)))
    if probe.code == 0:
        pytest.skip("git guesses a usable identity on this machine (a host name with a domain)")
    logged.clear()
    with pytest.raises(git_run.GitFailure):
        asyncio.run(git.run(["commit", "-qm", "c"], repo))
    assert logged[0] == "git commit failed (exit 128)"
    assert any("tell me who you are" in line.lower() or "auto-detect" in line for line in logged[1:]), logged


def test_no_lazy_fetch_sets_the_flag(tmp_path):
    assert git_run.git_environment(None, tmp_path, no_lazy_fetch=True)["GIT_NO_LAZY_FETCH"] == "1"


def test_missing_git_binary_is_missing_git(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(git_run.GitFailure) as failure:
        asyncio.run(git_run.git_version())
    assert failure.value.kind == "missing_git"
    git = git_run.Git(GH, None, tmp_path)
    with pytest.raises(git_run.GitFailure) as failure:
        asyncio.run(git.run(["status"], tmp_path))
    assert (failure.value.kind, failure.value.command, failure.value.code) == ("missing_git", "status", -1)


@pytest.mark.parametrize("body", ["echo 'xcrun: error: invalid active developer path' >&2; exit 1",
                                  "echo 'something else'; exit 0"])
def test_git_shim_whose_version_fails_is_broken_git(tmp_path, monkeypatch, body):
    monkeypatch.setenv("PATH", str(_fake_git(tmp_path, body)))
    with pytest.raises(git_run.GitFailure) as failure:
        asyncio.run(git_run.git_version())
    assert failure.value.kind == "broken_git"


def test_old_git_is_old_git_with_version(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(_fake_git(tmp_path, "echo 'git version 2.25.1'")))
    with pytest.raises(git_run.GitFailure) as failure:
        asyncio.run(git_run.git_version())
    assert (failure.value.kind, failure.value.version) == ("old_git", "2.25.1")


def test_current_git_version_is_digits_only(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(_fake_git(tmp_path, "echo 'git version 2.39.5 (Apple Git-154)'")))
    assert asyncio.run(git_run.git_version()) == "2.39.5"


@pytest.mark.parametrize("stderr, kind", [
    (b"fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com", "network"),
    (b"fatal: unable to access '...': SSL certificate problem", "network"),
    (b"fatal: unable to update url base from redirection", "redirect"),
    (b"remote: Permission to o/r.git denied to octo.\nfatal: unable to access: The requested URL returned error: 403",
     "forbidden"),
    (b"remote: Invalid username or token.\nfatal: Authentication failed for 'https://github.com/o/r.git/'", "auth"),
    (b"fatal: could not read Username for 'https://github.com': terminal prompts disabled", "auth"),
    (b"remote: Repository not found.\nfatal: repository 'https://github.com/o/r.git/' not found", "not_found"),
    (b"fatal: '/x' does not appear to be a git repository", "not_found"),
    (b"error: something unexpected happened", "other"),
    # A repository or path named like a failure never decides it.
    (b"remote: Repository not found.\nfatal: repository 'https://github.com/openssl/openssl.git/' not found",
     "not_found"),
    (b"remote: Invalid username or token.\nfatal: Authentication failed for 'https://github.com/rustls/rustls.git/'",
     "auth"),
    (b"remote: Permission to wolfssl/mbedtls.git denied to octo.\n"
     b"fatal: unable to access 'https://github.com/wolfssl/mbedtls.git/': The requested URL returned error: 403",
     "forbidden"),
    (b"remote: Repository not found.\nfatal: repository 'https://github.com/octo/redirect-service.git/' not found",
     "not_found"),
    (b"fatal: unable to access 'https://github.com/o/r.git/': error setting certificate file: /etc/ssl/certs/ca.pem",
     "network"),
    (b"fatal: unable to access 'https://github.com/o/r.git/': gnutls_handshake() failed", "network"),
    # SSH and GitLab phrasings, checked before the rules above.
    (b"Host key verification failed.\r\nfatal: Could not read from remote repository.", "host_key"),
    (b"@@@@@@@\n@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n", "host_key"),
    (b"Unable to negotiate with 1.2.3.4 port 22: no matching host key type found.", "host_key"),
    (b"Warning: the ECDSA host key for 'box' differs from the key for the IP address", "host_key"),
    (b'Load key "/tmp/collimate-git-1-x/ssh-y/key": invalid format\ngit@h: Permission denied (publickey).',
     "key"),
    (b"WARNING: UNPROTECTED PRIVATE KEY FILE!", "key"),
    (b"Load key: error in libcrypto", "key"),
    (b"git@gitlab.com: Permission denied (publickey).\nfatal: Could not read from remote repository.", "auth"),
    (b"remote: HTTP Basic: Access denied. If a password was provided for Git authentication, the password was "
     b"incorrect or you're required to use a token instead of a password.", "auth"),
    (b"ssh: connect to host box port 2222: Connection refused", "network"),
    (b"ssh: connect to host box port 22: No route to host", "network"),
    (b"Connection closed by 1.2.3.4 port 22", "network"),
    (b"kex_exchange_identification: read: Connection reset by peer", "network"),
    (b"Connection timed out during banner exchange", "network"),
    (b"remote: The project you were looking for could not be found or you don't have permission to view it.\n"
     b"fatal: repository 'https://gitlab.com/g/p.git/' not found", "not_found"),
    (b"remote: GitLab: You are not allowed to push code to this project.", "forbidden"),
    (b"ERROR: The key you are authenticating with has been marked as read only.\n"
     b"fatal: Could not read from remote repository.", "forbidden"),
])
def test_classify_table(stderr, kind):
    assert git_run.classify(stderr) == kind


def test_timeout_kills_child_and_reports_timeout(tmp_path, monkeypatch):
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("PATH", f"{_fake_git(tmp_path, f'echo $$ > {pid_file}; exec sleep 30')}:{os.environ['PATH']}")
    git = git_run.Git(GH, None, tmp_path)
    started = time.monotonic()
    with pytest.raises(git_run.GitFailure) as failure:
        asyncio.run(git.run(["status"], tmp_path, timeout=0.5))
    assert time.monotonic() - started < 10
    assert (failure.value.kind, failure.value.code) == ("timeout", -1)
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_failure_never_carries_stderr(tmp_path, git_spy):
    planted = f"fatal: Authentication failed for 'https://x-access-token:{TOKEN}@github.com/'".encode()
    git_spy.fail("fetch", code=128, stderr=planted * 100)
    git = git_run.Git(GH, AUTH, tmp_path)
    with pytest.raises(git_run.GitFailure) as failure:
        asyncio.run(git.run(["fetch", "origin"], tmp_path, network=True))
    exc = failure.value
    assert exc.kind == "auth" and exc.code == 128
    assert str(exc) == "git fetch failed (auth)"
    for text in (str(exc), repr(exc), repr(vars(exc)), repr(exc.args)):
        assert TOKEN not in text
    git_spy.fail("fetch", code=1, stderr=planted)
    completed = asyncio.run(git.run(["fetch", "origin"], tmp_path, network=True, ok=(0, 1)))
    assert completed.kind == "auth" and TOKEN not in repr(completed)


def test_failures_log_the_classification_then_gits_scrubbed_words(tmp_path, git_spy):
    """With a log, a failed command says what failed (classify's line), then
    git's own words with every form of the secret redacted; ``echo`` adds
    its output. Without one nothing is logged."""
    planted = (f"remote: Invalid username or token.\nfatal: Authentication failed for "
               f"'https://x-access-token:{TOKEN}@github.com/' ({AUTH.authorization})\n").encode()
    logged: list = []
    git = git_run.Git(GH, AUTH, tmp_path, log=logged.append, secrets=(TOKEN,))
    git_spy.fail("fetch", code=128, stderr=planted)
    with pytest.raises(git_run.GitFailure) as failure:
        asyncio.run(git.run(["fetch", "origin"], tmp_path, network=True))
    assert failure.value.kind == "auth"
    assert logged == ["the remote did not accept the credential for git fetch, or asked for one",
                      "remote: Invalid username or token.",
                      "fatal: Authentication failed for 'https://x-access-token:[redacted]@github.com/' ([redacted])"]
    logged.clear()
    git_spy.fail("pull", code=0, stdout=f"Updating a..b {TOKEN}\n".encode(), stderr=b"From github.com:octo/api\n")
    asyncio.run(git.run(["pull", "origin"], tmp_path, network=True, echo=True))
    assert logged == ["From github.com:octo/api", "Updating a..b [redacted]"]
    logged.clear()
    git_spy.fail("ls-remote", code=2, stderr=b"nothing\n")
    assert asyncio.run(git.run(["ls-remote", "origin"], tmp_path, network=True, ok=(0, 2))).code == 2
    assert logged == ["nothing"]
    quiet = git_run.Git(GH, AUTH, tmp_path)
    git_spy.fail("fetch", code=128, stderr=planted)
    with pytest.raises(git_run.GitFailure):
        asyncio.run(quiet.run(["fetch", "origin"], tmp_path, network=True))


def test_long_output_keeps_gits_last_words_from_a_lines_start(tmp_path, git_spy):
    """Past 64 KiB of stderr what is kept is the end, where git says what
    failed, beginning at a line: classify reads the fatal line, the log gets
    it, and a line the cut fell inside (half a secret) is dropped whole."""
    planted = (f"warning: {TOKEN}\n".encode() * 8000 + b"warning: x\n" * 3000
               + b"fatal: Authentication failed for 'https://github.com/octo/api.git/'\n")
    logged: list = []
    git = git_run.Git(GH, AUTH, tmp_path, log=logged.append, secrets=(TOKEN,))
    git_spy.fail("fetch", code=128, stderr=planted)
    with pytest.raises(git_run.GitFailure) as failure:
        asyncio.run(git.run(["fetch", "origin"], tmp_path, network=True))
    assert failure.value.kind == "auth"
    assert logged[0] == "the remote did not accept the credential for git fetch, or asked for one"
    assert logged[-1] == "fatal: Authentication failed for 'https://github.com/octo/api.git/'"
    assert sum(len(line) + 1 for line in logged[1:]) <= 64 * 1024
    assert all(line in ("warning: x", "warning: [redacted]") for line in logged[1:-1]), set(logged[1:-1])


def test_a_failure_before_git_runs_is_logged_too(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    logged: list = []
    with pytest.raises(git_run.GitFailure):
        asyncio.run(git_run.Git(GH, None, tmp_path, log=logged.append).run(["status"], tmp_path))
    assert logged == ["git is not installed on this machine"]


def test_every_failure_kind_has_its_line():
    for kind in git_run.FailureKind.__args__:
        line = git_run.failure_line(git_run.GitFailure(kind, "push", 128, version="2.30.1"))
        assert line and "{" not in line, kind
    assert git_run.failure_line(git_run.GitFailure("old_git", "--version", 0, version="2.30.1")) == (
        "git 2.30.1 on this machine is older than 2.31")


def test_config_pairs_follow_the_prefix(tmp_path, git_spy):
    git = git_run.Git(GH, None, tmp_path, config=("remote.origin.mirror=false",))
    git_spy.fail("push", code=0)
    asyncio.run(git.run(["push", "origin", "HEAD:refs/heads/main"], tmp_path, network=True))
    argv = list(git_spy.calls[-1][0])[1:]
    at = argv.index("remote.origin.mirror=false")
    assert argv[at - 1] == "-c" and argv[at + 1:] == ["push", "origin", "HEAD:refs/heads/main"]
    assert argv[:at - 1] == git_run.git_prefix(GH)


# ── HTTPS against a real git http-backend ──────────────────────────────


@pytest.mark.parametrize("username", ["x-access-token", "oauth2", "ada"])
def test_the_hosts_header_authenticates_only_inside_its_scope(tmp_path, monkeypatch, http_git, username):
    """The header comes from the run's environment as the host put it there,
    for a forge's username as for a server's: a clone under the account's
    base authenticates, an address outside it gets no header and meets the
    server's 401."""
    http_git.use(username)
    inside = http_git.seed("base/app.git")
    outside = http_git.seed("other/app.git")
    carry_account(monkeypatch, http_git.account)
    home = tmp_path / "home"
    home.mkdir()
    git = git_run.Git(git_run.Remote(inside, ("http",)), None, home)
    asyncio.run(git.run(["clone", "--quiet", "--", inside, str(home / "a")], home, network=True))
    assert (home / "a" / "f.txt").read_text() == "x\n"
    assert set(http_git.seen) == {f"Basic {http_git.account.authorization()}"}
    http_git.seen.clear()
    stray = git_run.Git(git_run.Remote(outside, ("http",)), None, home)
    with pytest.raises(git_run.GitFailure) as failure:
        asyncio.run(stray.run(["ls-remote", outside], home, network=True))
    assert failure.value.kind == "auth"
    assert set(http_git.seen) == {None}
    with pytest.raises(ValueError):
        git_run.Git(git_run.Remote(outside, ("http",)), http_git.account.auth_for(
            git_run.Remote(inside.replace("http://", "https://"), ("https",))), home)


def test_the_secret_lives_only_in_the_header_and_forge_names(tmp_path, monkeypatch, http_git, git_spy):
    """T4 for a run's git: a clone and a push authenticate with the scoped
    header alone. No argv, no file the clone leaves (its .git/config
    included, whose remote URL has no user info), and no logged line holds
    any form of the secret; in the environment only GIT_CONFIG_VALUE_<n>
    does."""
    url = http_git.seed("base/app.git")
    account = http_git.account
    carry_account(monkeypatch, account)
    forms = account.secret_forms()
    home = tmp_path / "home"
    home.mkdir()
    logged: list = []
    git = git_run.Git(git_run.Remote(url, ("http",)), None, home, log=logged.append, secrets=forms)
    work = home / "w"
    asyncio.run(git.run(["clone", "--", url, str(work)], home, network=True))
    (work / "g.txt").write_text("y\n")
    asyncio.run(git.run(["add", "g.txt"], work))
    asyncio.run(git.run(["commit", "-qm", "g"], work))
    asyncio.run(git.run(["push", "origin", "HEAD:refs/heads/main"], work, network=True, echo=True))
    assert subprocess.run(["git", "--git-dir", str(http_git.root / "base/app.git"), "log", "-1", "--format=%an <%ae>"],
                          capture_output=True, text=True).stdout.strip() == "Ada Lovelace <ada@example.org>"
    for argv, env in git_spy.calls:
        assert not any(form in word for form in forms for word in argv)
        assert [k for k, v in env.items() if any(form in v for form in forms)] == ["GIT_CONFIG_VALUE_0"]
    for path in tmp_path.rglob("*"):
        if path.is_file() and not path.is_symlink() and http_git.root not in path.parents:
            data = path.read_bytes()
            assert not any(form.encode() in data for form in forms), path
    assert subprocess.run(["git", "-C", str(work), "remote", "get-url", "origin"], capture_output=True,
                          text=True).stdout.strip() == url
    assert logged and not any(form in line for form in forms for line in logged)
    assert HTTP_TOKEN in forms


# ── the SSH key's lifetime ─────────────────────────────────────────────


def _ssh_account(monkeypatch) -> git_account.GitAccount:
    return _bound(monkeypatch, "git", "ssh_key", metadata={"known_hosts": BOX_LINE})


def _session_home(tmp: Path) -> Path:
    home = Path(tempfile.mkdtemp(prefix=f"collimate-git-{os.getpid()}-", dir=tmp))
    return home


@pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")
def test_ssh_key_lives_for_one_command(tmp_path, monkeypatch, fake_ssh, git_spy):
    acct = _ssh_account(monkeypatch)
    fake_ssh.hub.create(BOX.url, {"README.md": "hi\n"})
    home = _session_home(tmp_path)
    remote = git_run.resolve_remote(BOX.url, acct)
    git = git_run.Git(remote, acct.auth_for(remote), home)
    out = asyncio.run(git.run(["ls-remote", remote.url, "refs/heads/main"], home, network=True)).stdout
    assert out.endswith(b"\trefs/heads/main\n")
    call = fake_ssh.calls()[-1]
    assert (call["key_mode"], call["dir_mode"], call["dir_owner_is_uid"]) == (0o600, 0o700, True)
    import hashlib
    assert call["key_bytes_sha256"] == hashlib.sha256(acct.secret.encode()).hexdigest()
    assert call["known_hosts"] == [k.line() for k in acct.known_hosts]
    assert (call["host"], call["port"], call["user"], call["path"]) == ("box.example.org", 2222, "git", "/srv/app.git")
    assert "SSH_AUTH_SOCK" not in call["env_has"] and "GIT_CONFIG_COUNT" not in call["env_has"]
    assert list(home.iterdir()) == []
    _argv, env = git_spy.calls[-1]
    key_path = shlex.split(env["GIT_SSH_COMMAND"])[4]
    assert not os.path.exists(key_path)
    # A failing command (unknown repository), a timeout and a cancellation leave no key either.
    bad = git_run.Remote("ssh://git@box.example.org:2222/srv/missing.git", ("ssh",), BOX.known_host)
    with pytest.raises(git_run.GitFailure):
        asyncio.run(git_run.Git(bad, acct.auth_for(bad), home).run(["ls-remote", bad.url], home, network=True))
    assert list(home.iterdir()) == []
    release = tmp_path / "release"
    monkeypatch.setenv("FAKE_SSH_BLOCK", str(release))
    with pytest.raises(git_run.GitFailure) as failure:
        asyncio.run(git.run(["ls-remote", remote.url], home, network=True, timeout=1))
    assert failure.value.kind == "timeout"
    assert list(home.iterdir()) == []

    async def cancelled():
        task = asyncio.ensure_future(git.run(["clone", "--quiet", "--", remote.url, str(home / "c")], home,
                                             network=True))
        for _ in range(200):
            await asyncio.sleep(0.02)
            if any(home.glob("ssh-*/key")):
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancelled())
    assert not list(home.glob("ssh-*"))


def test_sweep_removes_only_dead_runs_session_directories(tmp_path, monkeypatch):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    dead = child.pid
    stale = tmp / f"collimate-git-{dead}-a"
    (stale / "ssh-x").mkdir(parents=True)
    (stale / "ssh-x" / "key").write_text("k")
    live = tmp / f"collimate-git-{os.getpid()}-b"
    (live / "ssh-y").mkdir(parents=True)
    (live / "ssh-y" / "key").write_text("k")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k")
    link = tmp / f"collimate-git-{dead}-c"
    link.symlink_to(outside)
    other = tmp / "collimate-git-x"
    other.mkdir()
    git_run.sweep_stale_sessions()
    assert not stale.exists()
    assert (live / "ssh-y" / "key").exists() and link.is_symlink() and (outside / "keep").exists() and other.exists()
    # A pid alive under another user (kill answers PermissionError) is kept.
    held = tmp / f"collimate-git-{dead}-d"
    held.mkdir()

    def denied(pid, sig):
        raise PermissionError(pid)
    monkeypatch.setattr(git_run.os, "kill", denied)
    git_run.sweep_stale_sessions()
    assert held.exists()


_CHILD = """
import asyncio, os, sys, tempfile
from pathlib import Path
sys.path[:0] = sys.argv[1:3]
from _env_shared import git_run
remote = git_run.Remote("ssh://git@box.example.org:2222/srv/app.git", ("ssh",), "[box.example.org]:2222")
auth = git_run.SshAuth(Path(sys.argv[3]).read_text(), (sys.argv[4],))
home = Path(tempfile.mkdtemp(prefix=f"collimate-git-{os.getpid()}-"))
asyncio.run(git_run.Git(remote, auth, home).run(["ls-remote", remote.url], home, network=True))
"""


@pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")
def test_killed_run_leaves_no_key_after_the_next_run(tmp_path, monkeypatch, fake_ssh):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    fake_ssh.hub.create(BOX.url, {"README.md": "hi\n"})
    release = tmp_path / "release"
    key_file = tmp_path / "key.txt"
    key_file.write_text(KEY)
    from tests._workspace import GENERATORS
    sdk_src = Path(git_hosts.__file__).resolve().parents[1]
    env = {**os.environ, "TMPDIR": str(tmp), "FAKE_SSH_BLOCK": str(release)}
    child = subprocess.Popen([sys.executable, "-c", _CHILD, str(GENERATORS), str(sdk_src), str(key_file), BOX_LINE],
                             env=env)
    try:
        for _ in range(500):
            if list(tmp.glob(f"collimate-git-{child.pid}-*/ssh-*/key")):
                break
            time.sleep(0.02)
        assert list(tmp.glob(f"collimate-git-{child.pid}-*/ssh-*/key"))
        child.send_signal(signal.SIGKILL)
        child.wait()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        release.touch()          # the orphaned git and fake ssh finish on their own
    assert list(tmp.glob(f"collimate-git-{child.pid}-*"))
    # The next Git run in this process sweeps the dead run's session (its key included).
    acct = _ssh_account(monkeypatch)
    monkeypatch.delenv("FAKE_SSH_BLOCK", raising=False)
    git_run.sweep_stale_sessions()
    assert not list(tmp.glob(f"collimate-git-{child.pid}-*"))
    with tempfile.TemporaryDirectory(prefix=f"collimate-git-{os.getpid()}-") as session:
        home = Path(session)
        remote = git_run.resolve_remote(BOX.url, acct)
        out = asyncio.run(git_run.Git(remote, acct.auth_for(remote), home).run(
            ["ls-remote", remote.url, "refs/heads/main"], home, network=True)).stdout
    assert out.endswith(b"\trefs/heads/main\n")
    assert not list(tmp.glob("collimate-git-*"))
