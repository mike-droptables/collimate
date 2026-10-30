# SPDX-License-Identifier: MIT
"""The hardened git process layer (``_env_shared.git_run``): no ambient
config, exactly one transport, the credential only where the account allows
it (an in-memory Basic header scoped to the account's base, or an SSH key in a
0600 file for one git command), and git's stderr never kept."""
from __future__ import annotations

import asyncio
import base64
import http.server
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from collimate import git_hosts
from _env_shared import git_account, git_run
from tests._git_fixtures import GIT_OK, bind_account, git_account_snapshot, ssh_key

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


def test_resolve_remote_per_record_and_account(monkeypatch):
    monkeypatch.setenv("GIT_REMOTE_OVERRIDE", "file:///tmp/x")
    assert git_run.resolve_remote(GH.url, None) == GH
    gh_ssh = _bound(monkeypatch, "github", "ssh_key")
    assert git_run.resolve_remote(GH.url, gh_ssh) == git_run.Remote(
        "ssh://git@github.com/octo/api.git", ("ssh",), "github.com")
    gl_ssh = _bound(monkeypatch, "gitlab", "ssh_key", endpoint="https://example.com/gitlab",
                    metadata={"known_hosts": BOX_LINE.replace("[box.example.org]:2222", "example.com")})
    assert git_run.resolve_remote("https://example.com/gitlab/group/sub/app.git", gl_ssh) == git_run.Remote(
        "ssh://git@example.com/group/sub/app.git", ("ssh",), "example.com")
    # A record outside the forge's base stays https (auth_for then refuses it).
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


def test_environment_scrubs_ambient_git_tokens_askpass_and_ssh(tmp_path, monkeypatch):
    ambient = {"GIT_TRACE": "1", "GIT_CONFIG_KEY_17": "credential.helper", "GIT_ASKPASS": "/x",
               "GIT_SSH_COMMAND": "ssh -v", "GIT_SSH": "/s", "HTTPS_PROXY": "http://proxy:3128",
               **{name: "planted" for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                                               "GITHUB_ENTERPRISE_TOKEN", "GH_HOST", "GITLAB_TOKEN", "GITLAB_HOST",
                                               "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "SSH_AUTH_SOCK")}}
    for key, value in ambient.items():
        monkeypatch.setenv(key, value)
    for auth in (None, AUTH, SSH_AUTH):
        env = git_run.git_environment(auth, tmp_path)
        assert not (git_run._DROPPED | {"GIT_TRACE", "GIT_CONFIG_KEY_17", "GIT_ASKPASS", "GIT_SSH_COMMAND",
                                        "GIT_SSH"}) & env.keys()
        assert "planted" not in env.values()
        assert env["HTTPS_PROXY"] == "http://proxy:3128"
        assert env["HOME"] == env["XDG_CONFIG_HOME"] == str(tmp_path)
        assert (env["GIT_CONFIG_NOSYSTEM"], env["GIT_CONFIG_GLOBAL"], env["GIT_TERMINAL_PROMPT"]) == ("1", os.devnull, "0")
        assert (env["GIT_ADVICE"], env["LC_ALL"], env["LANGUAGE"]) == ("0", "C", "")


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


def test_identity_sets_author_and_committer(tmp_path):
    env = git_run.git_environment(None, tmp_path, git_run.Identity("Ada Lovelace", "ada@example.org"))
    assert (env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"]) == ("Ada Lovelace", "ada@example.org")
    assert (env["GIT_COMMITTER_NAME"], env["GIT_COMMITTER_EMAIL"]) == ("Ada Lovelace", "ada@example.org")
    default = git_run.git_environment(None, tmp_path)
    assert (default["GIT_AUTHOR_NAME"], default["GIT_AUTHOR_EMAIL"]) == ("Collimate", "collimate@localhost.invalid")


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


# ── HTTPS against a real git http-backend ──────────────────────────────


def _http_backend() -> "str | None":
    try:
        exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    backend = Path(exec_path, "git-http-backend")
    return str(backend) if backend.is_file() else None


class _Backend(http.server.BaseHTTPRequestHandler):
    """CGI front for git http-backend: 401 unless the Basic credential matches."""
    root: Path
    expected: str
    backend: str

    def log_message(self, *args):
        pass

    def _serve(self):
        if self.headers.get("Authorization") != f"Basic {self.expected}":
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        env = {"GIT_PROJECT_ROOT": str(self.root), "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path,
               "QUERY_STRING": query, "REQUEST_METHOD": self.command, "REMOTE_ADDR": "127.0.0.1",
               "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
               "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""), "PATH": os.environ.get("PATH", "")}
        out = subprocess.run([self.backend], input=body, env=env, capture_output=True, timeout=60).stdout
        head, _, payload = out.partition(b"\r\n\r\n")
        status = 200
        headers = []
        for line in head.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.split()[0])
            elif name:
                headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve


@pytest.mark.skipif(not GIT_OK or _http_backend() is None, reason="needs git 2.31+ with git-http-backend")
@pytest.mark.parametrize("username", ["x-access-token", "oauth2", "ada"])
def test_basic_header_authenticates_only_inside_its_scope(tmp_path, monkeypatch, username):
    root = tmp_path / "srv"
    for name in ("base/app.git", "other/app.git"):
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(root / name)], check=True, capture_output=True)
        seed = tmp_path / f"seed-{name.split('/')[0]}"
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(seed)], check=True, capture_output=True)
        (seed / "f.txt").write_text("x\n")
        env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": "a",
               "GIT_COMMITTER_EMAIL": "a@b", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        for args in (["add", "f.txt"], ["commit", "-qm", "c"], ["push", "-q", str(root / name), "main"]):
            subprocess.run(["git", *args], cwd=seed, env=env, check=True, capture_output=True)
    authorization = git_run.basic_authorization(username, TOKEN)
    handler = type("Handler", (_Backend,), {"root": root, "expected": authorization, "backend": _http_backend()})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        monkeypatch.setattr(git_run, "_HEADER_PROTOCOLS", ("https", "http"))
        auth = git_run.HttpsAuth(f"http://127.0.0.1:{port}/base/", authorization)
        inside = git_run.Remote(f"http://127.0.0.1:{port}/base/app.git", ("http",))
        home = tmp_path / "home"
        home.mkdir()
        git = git_run.Git(inside, auth, home)
        asyncio.run(git.run(["clone", "--quiet", "--", inside.url, str(home / "a")], home, network=True))
        assert (home / "a" / "f.txt").read_text() == "x\n"
        outside = git_run.Remote(f"http://127.0.0.1:{port}/other/app.git", ("http",))
        with pytest.raises(ValueError):
            git_run.Git(outside, auth, home)
        # Same account, wrong address: the header is scoped, so nothing is sent and git meets the 401.
        stray = git_run.Git(inside, auth, home)
        with pytest.raises(git_run.GitFailure) as failure:
            asyncio.run(stray.run(["ls-remote", outside.url], home, network=True))
        assert failure.value.kind == "auth"
    finally:
        server.shutdown()
        server.server_close()


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
    from collimate import native
    from collimate.testing import make_test_bridge
    from _env_shared import repositories
    acct = _ssh_account(monkeypatch)
    monkeypatch.delenv("FAKE_SSH_BLOCK", raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bridge = make_test_bridge(workspace, permissions={"internet": "full"})
    native.set_bridge(bridge)
    try:
        report = asyncio.run(repositories.sync(repositories.Target(BOX.url, "app"), account=acct,
                                               log=lambda _line: None))
    finally:
        native._clear_bridge()
        bridge.close()
    assert report.kind == "cloned"
    assert not list(tmp.glob(f"collimate-git-{child.pid}-*"))
    assert not list(tmp.glob("collimate-git-*"))
