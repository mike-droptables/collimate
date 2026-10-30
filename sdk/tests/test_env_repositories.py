# SPDX-License-Identifier: MIT
"""The repository engine (``_env_shared.repositories``) against bare
repositories standing in for every remote: the prompt grammar and the record's
remote rule, folder states, clone, update, push, the two-phase commit, the
account's reach (A1–A5), SSH through a fake ssh, and secret hygiene."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

import gen
from collimate import git_hosts
from _env_shared import git_account, git_run, repositories
from tests._git_fixtures import (GH, GIT_OK, GL, SSH, bind_account, git_account_snapshot, ssh_key,
                                 subcommand_of)

needs_git = pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")
IDENT = git_run.Identity("Ada Lovelace", "ada@example.org")
TOKEN = "ghp_engine_s3cret"
ACCOUNT = git_account.GitAccount("github", "api_key", "https://github.com", "x-access-token", TOKEN, (), IDENT)
D = "github.com/octo/api"                  # display_of(GH)
T = repositories.Target
_ED = git_hosts.PINNED_HOST_KEYS["github.com"][0].split(" ", 1)[1]     # "ssh-ed25519 <blob>"
BOX_KNOWN = f"[box.example.org]:2222 {_ED}\nbox.example.org {_ED}"


def make_account(service="github", auth="api_key", **over) -> git_account.GitAccount:
    """A GitAccount as git_account() builds it from a snapshot."""
    snapshot = git_account_snapshot(service, auth, **over)
    gen_account = gen.account
    try:
        gen.account = lambda role: snapshot
        return git_account.git_account()
    finally:
        gen.account = gen_account


@pytest.fixture
def cell(tmp_path):
    """A host-free run whose workspace is ``tmp_path/ws`` (beside, never
    holding, the fake remote)."""
    from collimate import native
    from collimate.testing import make_test_bridge
    ws = tmp_path / "ws"
    ws.mkdir()
    bridge = make_test_bridge(ws, permissions={"internet": "full"})
    native.set_bridge(bridge)
    try:
        yield bridge
    finally:
        native._clear_bridge()
        bridge.close()


@pytest.fixture
def ws(cell) -> Path:
    return Path(cell.workspace)


@pytest.fixture
def logs() -> list:
    return []


@pytest.fixture
def private_tmp(tmp_path, monkeypatch) -> Path:
    """The system temp folder the engine's temporary clones go to."""
    path = tmp_path / "systemp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


def sync(target, logs=None, **kw):
    kw.setdefault("account", None)
    return asyncio.run(repositories.sync(target, log=(logs.append if logs is not None else _drop), **kw))


def push(target, logs=None, **kw):
    kw.setdefault("account", ACCOUNT)
    kw.setdefault("message", None)
    return asyncio.run(repositories.push(target, log=(logs.append if logs is not None else _drop), **kw))


def _drop(_line):
    pass


def snapshot(ws: Path) -> dict:
    out = {}
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if not (Path(root) == ws and d == ".collrun")]
        for name in files:
            path = Path(root, name)
            out[path.relative_to(ws).as_posix()] = path.read_bytes()
    return out


def folder_files(ws: Path, folder: str) -> dict:
    return {p.removeprefix(folder + "/"): data for p, data in snapshot(ws).items()
            if p.startswith(folder + "/")}


def marker(ws: Path, folder: str = "api") -> dict:
    return json.loads((ws / repositories.marker_path(folder)).read_text())


def write_marker(ws: Path, folder: str, data, *, make_folder: bool = True) -> None:
    if make_folder:
        (ws / folder).mkdir(exist_ok=True)
    body = data if isinstance(data, (str, bytes)) else json.dumps(data)
    path = ws / repositories.marker_path(folder)
    path.write_bytes(body if isinstance(body, bytes) else body.encode())


BASE = {"README.md": "hello\n", "src/a.py": "a = 1\n", "src/b.py": "b = 1\n", "docs/c.txt": "c\n"}


def cloned(fake_remote, ws, remote=GH, files=None, **kw):
    fake_remote.create(remote, dict(files or BASE), **{k: v for k, v in kw.items() if k == "branch"})
    target = T(remote, repositories.default_folder(remote))
    sync(target, **{k: v for k, v in kw.items() if k != "branch"})
    return target


def spawned(git_spy) -> list[str]:
    return [s for s in git_spy.subcommands() if s != "--version"]


# ── the prompt and the record's remote ───────────────────────────────

GHE = make_account(endpoint="https://ghe.example.com")
GLA = make_account("gitlab", endpoint="https://example.com/gitlab")
BASIC = make_account("git", "basic")
NONE = make_account("git", "none")
BOX = make_account("git", "ssh_key", metadata={"known_hosts": BOX_KNOWN})
GH_SSH = make_account("github", "ssh_key")


@pytest.mark.parametrize("account, value, expected", [
    # unbound: an address; github.com and gitlab.com take the forge form
    (None, "https://github.com/octo/api", GH),
    (None, "  https://github.com/octo/api.git/  ", GH),
    (None, "HTTPS://GitHub.com/Octo/API", "https://github.com/Octo/API.git"),
    (None, "https://github.com/octo/.github", "https://github.com/octo/.github.git"),
    (None, "https://github.com/octo/api/tree/main/src", GH),
    (None, "https://gitlab.com/g/s/app/-/blob/x/README.md", "https://gitlab.com/g/s/app.git"),
    (None, "https://git.example.org/x/y.git", "https://git.example.org/x/y.git"),
    (None, "https://git.example.org/x/y", "https://git.example.org/x/y"),
    (None, "https://git.example.org:443/x/y.git", "https://git.example.org/x/y.git"),
    (None, "https://git.example.org:8443/x/y/", "https://git.example.org:8443/x/y"),
    (None, "https://dev.azure.com/org/My%20Project/_git/app", "https://dev.azure.com/org/My%20Project/_git/app"),
    (None, "https://git.example.org/a/tree/main", "https://git.example.org/a/tree/main"),
    # GitHub account (github.com and GHE)
    (ACCOUNT, "octo/api", GH),
    (ACCOUNT, "octo/api.git", GH),
    (ACCOUNT, "https://github.com/octo/api/pull/3", GH),
    (ACCOUNT, "git@github.com:octo/api.git", GH),
    (ACCOUNT, "ssh://git@github.com/octo/api", GH),
    (ACCOUNT, "git@github.com:/octo/api.git", GH),
    (GHE, "team/app", "https://ghe.example.com/team/app.git"),
    (GH_SSH, "octo/api", GH),
    # GitLab account under a relative URL: nested groups
    (GLA, "group/sub/app", GL),
    (GLA, "https://example.com/gitlab/group/sub/app/-/tree/main", GL),
    (GLA, "git@example.com:group/sub/app.git", GL),
    (GLA, "ssh://git@example.com:22/group/sub/app", GL),
    # Git server, username and token
    (BASIC, "team/app.git", "https://git.example.org/team/app.git"),
    (BASIC, "https://git.example.org/team/app", "https://git.example.org/team/app"),
    # Git server, public
    (NONE, "https://gitlab.com/g/p", "https://gitlab.com/g/p.git"),
    (NONE, "https://anywhere.example/r.git", "https://anywhere.example/r.git"),
    # Git server, SSH key: ssh:// stays ssh://, scp stays scp byte for byte
    (BOX, "ssh://git@box.example.org:2222/srv/app.git", SSH),
    (BOX, "ssh://box.example.org:2222/srv/app.git", SSH),
    (BOX, "ssh://git@box.example.org:22/r", "ssh://git@box.example.org/r"),
    (BOX, "git@box.example.org:team/r.git", "git@box.example.org:team/r.git"),
    (BOX, "git@box.example.org:~/r.git", "git@box.example.org:~/r.git"),
    (BOX, "git@box.example.org:/srv/r.git", "git@box.example.org:/srv/r.git"),
    (BOX, "box.example.org:team/r.git", "git@box.example.org:team/r.git"),
    (BOX, "git@Box.Example.org:team/r/", "git@box.example.org:team/r"),
])
def test_parse_prompt_forms(account, value, expected):
    lines: list = []
    assert repositories.parse_prompt(value, account, log=lines.append) == expected
    assert repositories.check_remote(expected)
    if "/tree/" in value and "github.com" in value or "/-/" in value or "/pull/" in value:
        assert lines == [f"Using {repositories.display_of(expected)} (the rest of the address is a page, not the "
                         "repository)"]
    else:
        assert lines == []


def test_parse_prompt_uppercase_ssh_user_and_generic_dot_git_kept():
    code_commit = make_account("git", "ssh_key", metadata={
        "known_hosts": f"git-codecommit.us-east-1.amazonaws.com {_ED}"})
    value = "APKAEIBAERJR2EXAMPLE@git-codecommit.us-east-1.amazonaws.com:v1/repos/app"
    assert repositories.parse_prompt(value, code_commit) == value
    assert (repositories.parse_prompt("https://git.example.org/x/y.git", None)
            != repositories.parse_prompt("https://git.example.org/x/y", None))


@pytest.mark.parametrize("account, value, message", [
    (None, "", repositories.I1),
    (None, "octo/api", repositories.I1b),
    (NONE, "octo/api", repositories.I1c),
    (BOX, "srv/app", repositories.I1c),
    (None, "https://u:ghp_x@github.com/o/r", repositories.I2),
    (None, "https://ghp_x@github.com/o/r", repositories.I2),
    (None, "https://github.com/o/r?ghp_x=1", repositories.I2),
    (None, "https://github.com/o/r#ghp_x", repositories.I2),
    (BOX, "ssh://git:ghp_x@box.example.org/r", repositories.I2),
    (BOX, "git:ghp_x@box.example.org:r", repositories.I2),
    (None, "http://github.com/o/ghp_x", repositories.I2b),
    (None, "git://github.com/o/ghp_x", repositories.I2b),
    (None, "file:///tmp/ghp_x", repositories.I2b),
    (None, "ext::sh -c ghp_x", repositories.I2b),
    (None, "/srv/ghp_x.git", repositories.I2b),
    (ACCOUNT, "org/../ghp_x", repositories.I3g),
    (ACCOUNT, "org/..", repositories.I3),
    (ACCOUNT, "-o/r", repositories.I3),
    (ACCOUNT, "o/r/ghp_x", repositories.I3g),
    (ACCOUNT, "git@github.com:o/r/ghp_x.git", repositories.I3g),
    (ACCOUNT, "ghp_x", repositories.I3g),
    (None, "https://github.com/ghp_x", repositories.I3g),
    (GLA, "ghp_x", repositories.I3),
    (GLA, "g/ghp_x.atom", repositories.I3),
    (GLA, "git@example.com:~/ghp_x.git", repositories.I3),
    (None, "https://[::1]/ghp_x", repositories.I3),
    (None, "https://git.example.org/o/../ghp_x", repositories.I3),
    (BOX, "ssh://-oProxyCommand=ghp_x/r", repositories.I3),
    (BOX, "git@box.example.org:-ghp_x", repositories.I3),
    (ACCOUNT, "https://gitlab.com/g/ghp_x", repositories.A1.format(base="github.com", host="gitlab.com")),
    (GLA, "https://example.com/other/ghp_x", repositories.A1.format(base="example.com/gitlab", host="example.com")),
    (GLA, "ssh://git@example.com:2222/g/ghp_x", repositories.A1.format(base="example.com/gitlab",
                                                                      host="example.com:2222")),
    (BASIC, "https://other.example/ghp_x", repositories.A1.format(base="git.example.org", host="other.example")),
    (None, "git@box.example.org:team/ghp_x.git", repositories.A2.format(r="box.example.org:team/ghp_x")),
    (BASIC, "git@git.example.org:team/ghp_x", repositories.A3.format(method="a username and token", scheme="SSH",
                                                                     other="https")),
    (NONE, "ssh://git@h.example/ghp_x", repositories.A3.format(method="no credentials", scheme="SSH", other="https")),
    (BOX, "https://box.example.org/ghp_x", repositories.A3.format(method="an SSH key", scheme="https", other="SSH")),
    (BOX, "git@other.example:ghp_x", repositories.A4.format(hostport="other.example")),
    (BOX, "ssh://git@box.example.org:2200/ghp_x", repositories.A4.format(hostport="[box.example.org]:2200")),
])
def test_parse_prompt_refusals_never_echo_input(account, value, message):
    with pytest.raises(ValueError) as failure:
        repositories.parse_prompt(value, account, log=_drop)
    assert str(failure.value) == message
    if message not in (repositories.A2.format(r="box.example.org:team/ghp_x"),):
        assert "ghp_x" not in str(failure.value)


def test_parse_prompt_requires_exactly_one_line():
    for blank in (None, "", "  \n\t\n"):
        with pytest.raises(ValueError) as failure:
            repositories.parse_prompt(blank, None)
        assert str(failure.value) == repositories.I1
    with pytest.raises(ValueError) as failure:
        repositories.parse_prompt("octo/api\n\nocto/web\n", ACCOUNT)
    assert str(failure.value) == "The prompt names one repository; it has 2 lines"
    assert repositories.parse_prompt("\n  https://github.com/octo/api  \n", None) == GH


def test_check_remote_and_remote_key_and_display():
    for good in (GH, GL, SSH, "https://github.com/octo/.github.git", "https://gitserver/r.git",
                 "ssh://git@host:2222/~/r.git", "ssh://deploy@host/~deploy/r", "git@host:team/r.git",
                 "git@host:~/r.git", "git@host:/srv/r.git", "deploy@host:~deploy/r",
                 "https://dev.azure.com/org/My%20Project/_git/app", "https://h/" + "/".join(["s"] * 32)):
        assert repositories.check_remote(good), good
    for bad in ("octo/api", "http://github.com/o/a", "HTTPS://github.com/o/a", "https://GitHub.com/o/a",
                "https://github.com:443/o/a", "ssh://git@h:22/r", "https://u@github.com/o/a", "ssh://git:p@h/r",
                "ssh://h/r", "ssh://-oProxyCommand=x/r", "https://github.com/o/a?x=1", "https://github.com/o/../a",
                "https://github.com/o/a/", "https://github.com/~/r", "ssh://git@h/r/~/x", "host:team/r.git",
                "git@host:-x", "git@Host:r", "git:p@host:r", "-git@host:r", "https://h/a%2Fb", "https://h/a%2fb",
                "https://h/%2E%2E/x", "https://h:65536/x", "https://h/" + "/".join(["s"] * 33), "x" * 513, 7, None):
        assert not repositories.check_remote(bad), bad
    assert repositories.remote_key("https://github.com/Octo/API.git") == repositories.remote_key(GH.removesuffix(".git"))
    assert repositories.display_of(GH) == "github.com/octo/api"
    assert repositories.display_of(SSH) == "box.example.org:2222/srv/app"
    assert repositories.display_of("git@box.example.org:team/api.git") == "box.example.org:team/api"
    assert repositories.host_of(SSH) == "box.example.org:2222"
    assert repositories.host_of("git@box.example.org:team/api.git") == "box.example.org"
    assert repositories.host_of(GL) == "example.com"


def test_check_folder_rules():
    for good in ("api", "my.repo", "_x", ".hidden", "a-b_c.d", "x" * 100):
        assert repositories.check_folder(good) == good
    for bad in (".", "..", "a/b", "", "a b", "-x", "x" * 101, "ghp_x/../y"):
        with pytest.raises(ValueError) as failure:
            repositories.check_folder(bad)
        assert str(failure.value) == repositories.I5
    for reserved in (".git", ".COLLRUN", "_runs", "_Agent", "node_modules", ".venv", "__pycache__"):
        with pytest.raises(ValueError) as failure:
            repositories.check_folder(reserved)
        assert str(failure.value) == repositories.I5b
    assert repositories.default_folder(GH) == "api"
    assert repositories.default_folder(SSH) == "app"
    assert repositories.default_folder("git@box.example.org:~/r.git") == "r"
    for name in ("https://github.com/octo/_runs.git", "https://github.com/octo/node_modules.git",
                 "https://github.com/octo/.venv.git"):
        with pytest.raises(ValueError) as failure:
            repositories.default_folder(name)
        assert str(failure.value) == repositories.I5c


_BRANCHES = ["main", "feature/x", "release-1.0", "a.b", "user@x", "@x", "a{b", "a}b", "ünïcödé", "x" * 255,
             "-x", "/x", "x/", "x.", "a..b", "a//b", "a@{b", "a b", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b",
             "a\\b", ".x", "x/.y", "x.lock", "x/y.lock/z", "a\x01b", "a\x7fb", "HEAD"]


@needs_git
def test_check_branch_agrees_with_git_check_ref_format(tmp_path):
    for name in _BRANCHES:
        git_ok = subprocess.run(["git", "check-ref-format", "--branch", name], cwd=tmp_path,
                                capture_output=True).returncode == 0
        try:
            ours = repositories.check_branch(name) == name
        except ValueError as exc:
            assert str(exc) == repositories.I6
            ours = False
        assert ours == git_ok, name
    for stricter in ("@", "", "x" * 256):
        with pytest.raises(ValueError):
            repositories.check_branch(stricter)


def test_marker_exact_bytes():
    sha = "3f786850e387550fdab836ed7e6dc881de23001b"
    assert repositories.Marker("https://github.com/octo/api.git", "main", sha).text() == (
        '{\n  "about": "Git checkout record: the folder of the same name beside this file holds this '
        "repository's files at this commit plus the cell's edits, without .git. Before any git operation (diff, "
        "log, blame, compare, sync or push) read the git-checkout handler's skill (handler_skills tool, id "
        'git-checkout).",\n  "remote": "https://github.com/octo/api.git",\n  "branch": "main",\n'
        '  "commit": "3f786850e387550fdab836ed7e6dc881de23001b"\n}\n')
    assert len(repositories.ABOUT) == 292 <= repositories.ABOUT_CAP


_KEYS = "not an object with exactly about, remote, branch and commit"
_A = repositories.ABOUT


@pytest.mark.parametrize("body, reason", [
    ({"about": _A, "remote": GH, "commit": "a" * 40}, _KEYS),
    ({"remote": GH, "branch": "main", "commit": "a" * 40}, _KEYS),
    ({"about": _A, "remote": GH, "branch": "main", "commit": "a" * 40, "x": 1}, _KEYS),
    ({"about": _A, "repository": "octo/api", "branch": "main", "commit": "a" * 40}, _KEYS),
    ([GH], _KEYS),
    ({"about": "", "remote": GH, "branch": "main", "commit": "a" * 40}, "bad about"),
    ({"about": "x" * 501, "remote": GH, "branch": "main", "commit": "a" * 40}, "bad about"),
    ({"about": 7, "remote": GH, "branch": "main", "commit": "a" * 40}, "bad about"),
    ({"about": _A, "remote": GH, "branch": "main", "commit": "abc"}, "bad commit"),
    ({"about": _A, "remote": GH, "branch": "main", "commit": "--upload-pack=x"}, "bad commit"),
    ({"about": _A, "remote": GH, "branch": "a..b", "commit": "a" * 40}, "bad branch"),
    ('{"about":"x","remote":"https://h.io/o/r","branch":"\\ud800","commit":"' + "a" * 40 + '"}', "bad branch"),
    ({"about": _A, "remote": "octo/api", "branch": "main", "commit": "a" * 40}, "bad remote"),
    ({"about": _A, "remote": "https://u:p@github.com/o/a", "branch": "main", "commit": "a" * 40}, "bad remote"),
    ({"about": _A, "remote": "ssh://-oProxyCommand=x/r", "branch": "main", "commit": "a" * 40}, "bad remote"),
    ({"about": _A, "remote": "http://github.com/o/a", "branch": "main", "commit": "a" * 40}, "bad remote"),
    ({"about": _A, "remote": 7, "branch": "main", "commit": "a" * 40}, "bad remote"),
    ('{"about":"x","remote":"https://h.io/o/r","branch":"main","commit":NaN}', "not UTF-8 JSON"),
    (b"\xff\xfe{}", "not UTF-8 JSON"),
    ("{not json", "not UTF-8 JSON"),
    ("x" * 5000, "larger than 4 KB"),
])
def test_marker_refusals(tmp_path, body, reason):
    write_marker(tmp_path, "api", body)
    with pytest.raises(repositories.RepositoryError) as failure:
        repositories.read_marker(tmp_path, "api")
    assert str(failure.value) == repositories.F3.format(f="api", reason=reason)


def test_marker_symlink_is_refused(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "elsewhere.json").write_text(repositories.Marker(GH, "main", "a" * 40).text())
    (tmp_path / "api.git-checkout.json").symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(repositories.RepositoryError, match=r"\(not a regular file\)"):
        repositories.read_marker(tmp_path, "api")
    assert repositories.read_marker(tmp_path, "missing") is None


# ── resolution and state ─────────────────────────────────────────────

VALID = {"about": repositories.ABOUT, "remote": "https://github.com/Octo/API.git", "branch": "main", "commit": "a" * 40}


def test_resolve_target_reuses_a_folder_cloned_under_another_name(tmp_path):
    write_marker(tmp_path, "custom", VALID)
    write_marker(tmp_path, "broken", "{}")
    (tmp_path / "plain").mkdir()
    lines: list = []
    assert repositories.resolve_target(tmp_path, GH, log=lines.append) == T(GH, "custom")
    assert lines == ["Ignored folder broken/: broken.git-checkout.json is not a valid checkout record"]
    assert repositories.resolve_target(tmp_path, "https://github.com/octo/web.git", log=lines.append) == T("https://github.com/octo/web.git", "web")
    assert repositories.resolve_target(tmp_path, GH, "other", log=lines.append) == T(GH, "other")
    with pytest.raises(ValueError, match="^Folder must be"):
        repositories.resolve_target(tmp_path, GH, "a/b", log=lines.append)


def test_resolve_target_refuses_two_folders_for_one_repository(tmp_path):
    write_marker(tmp_path, "one", VALID)
    write_marker(tmp_path, "two", {**VALID, "remote": GH})
    with pytest.raises(repositories.RepositoryError) as failure:
        repositories.resolve_target(tmp_path, GH, log=_drop)
    assert str(failure.value) == ("Several folders hold github.com/octo/api (one/, two/); set Folder to choose one. "
                                  "Nothing changed")


@pytest.mark.parametrize("state, expected", [
    ("S1-file", "api is a file in this cell, not a repository folder"),
    ("S1-link", "api is a file in this cell, not a repository folder"),
    ("S2", "Folder api/ already exists and was not cloned by Git Clone / Update"),
    ("S3", "api.git-checkout.json is not a valid checkout record (bad commit)"),
    ("S4", "Folder api/ holds github.com/octo/other, not github.com/octo/api"),
    ("S5", "Folder api/ follows branch main, not dev"),
    ("S6", "Folder API/ already exists and differs from api only in case"),
])
@pytest.mark.parametrize("operation", ["sync", "push"])
def test_folder_states_refuse_before_any_git_call(ws, git_spy, state, expected, operation):
    good = {"about": repositories.ABOUT, "remote": GH, "branch": "main", "commit": "a" * 40}
    if state == "S1-file":
        (ws / "api").write_text("not a folder")
    elif state == "S1-link":
        (ws / "real").mkdir()
        (ws / "api").symlink_to(ws / "real")
    elif state == "S2":
        (ws / "api").mkdir()
    elif state == "S3":
        write_marker(ws, "api", {**good, "commit": "xyz"})
    elif state == "S4":
        write_marker(ws, "api", {**good, "remote": "https://github.com/octo/other.git"})
    elif state == "S5":
        write_marker(ws, "api", good)
    elif state == "S6":
        (ws / "API").mkdir()
    before = snapshot(ws)
    branch = "dev" if state == "S5" else ""
    with pytest.raises(repositories.RepositoryError) as failure:
        if operation == "sync":
            sync(T(GH, "api"), branch=branch)
        else:
            push(T(GH, "api"), branch=branch)
    assert str(failure.value).startswith(expected)
    assert str(failure.value).endswith("Nothing changed")
    assert git_spy.calls == []
    assert snapshot(ws) == before


def test_push_of_a_folder_never_cloned_is_f7(ws, git_spy):
    with pytest.raises(repositories.RepositoryError) as failure:
        push(T(GH, "api"))
    assert str(failure.value) == "Folder api/ has no clone of github.com/octo/api; run Git Clone / Update first. Nothing pushed"
    assert git_spy.calls == []


# ── environment ───────────────────────────────────────────────────────


def _fake_git_bin(tmp_path, body):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "git"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return bin_dir


@pytest.mark.parametrize("case, expected", [
    ("offline", repositories.E1),
    ("missing", repositories.E2),
    ("broken", repositories.E3),
    ("old", "git 2.25.1 is too old; Git runs need git 2.31 or later"),
])
def test_preflight_refuses_offline_missing_broken_and_old_git(cell, tmp_path, monkeypatch, case, expected):
    monkeypatch.setattr(repositories, "_git_version", None)
    if case == "offline":
        cell.permissions = {"internet": "none"}
    elif case == "missing":
        monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    elif case == "broken":
        monkeypatch.setenv("PATH", str(_fake_git_bin(tmp_path, "exit 1")))
    else:
        monkeypatch.setenv("PATH", str(_fake_git_bin(tmp_path, "echo 'git version 2.25.1'")))
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(GH, "api"))
    assert str(failure.value) == expected
    assert not (Path(cell.workspace) / "api").exists()
    assert repositories._git_version is None


# ── clone ─────────────────────────────────────────────────────────────


@needs_git
def test_clone_is_blobless_single_branch_and_never_places_git(ws, fake_remote, git_spy):
    fake_remote.create(GH, BASE)
    report = sync(T(GH, "api"))
    assert report.kind == "cloned"
    assert spawned(git_spy)[:2] == ["ls-remote", "clone"]
    clone = next(argv for argv, _env in git_spy.calls if subcommand_of(argv) == "clone")
    for flag in ("--filter=blob:none", "--single-branch", "--no-tags", "--no-checkout", "--no-recurse-submodules"):
        assert flag in clone
    assert not any(a.startswith("--depth") or a.startswith("--shallow") for a in clone)
    assert not any(".git" in Path(p).parts for p in snapshot(ws))
    assert folder_files(ws, "api") == {p: d.encode() for p, d in BASE.items()}


@needs_git
def test_clone_moves_and_tracks_files_then_commits_marker_in_a_second_commit(cell, ws, fake_remote, monkeypatch):
    head = fake_remote.create(GH, BASE)
    tracked, rewritten = [], []
    real_track, real_update = gen.track_file, gen.update_file
    monkeypatch.setattr(gen, "track_file", lambda path, **kw: (tracked.append((path, kw)), real_track(path, **kw)))
    monkeypatch.setattr(gen, "update_file", lambda path, data, **kw: (rewritten.append(path),
                                                                     real_update(path, data, **kw)))
    logs: list = []
    report = sync(T(GH, "api"), logs=logs)
    assert sorted(p for p, _ in tracked) == sorted(f"api/{p}" for p in BASE)
    assert all(kw == {"max_bytes": repositories.FILE_CAP} for _, kw in tracked)
    assert rewritten == ["api.git-checkout.json"]
    assert [sorted(u["files"]) for u in cell.updates] == [sorted(f"api/{p}" for p in BASE),
                                                          ["api.git-checkout.json"]]
    assert marker(ws) == repositories.Marker(GH, "main", head).data()
    assert (ws / repositories.marker_path("api")).read_text() == repositories.Marker(GH, "main", head).text()
    assert report == repositories.SyncReport("cloned", T(GH, "api"), "main", None, head, 0, 4, 0, 0, 0)
    assert report.note() == f"Cloned github.com/octo/api main@{head[:7]} into api/: 4 files"
    assert logs[-1] == report.note()
    assert "Checking github.com/octo/api" in logs and "Cloning github.com/octo/api main into api/" in logs


KEPT = {"README.md": "hi\n", "link": ("link", "README.md"), "sub": ("gitlink", "a" * 40),
        "big.bin": b"x" * 2000, "node_modules/x.js": "x", ".venv/y.py": "y", "pkg/__pycache__/z.pyc": b"z",
        "pkg/mod.py": "m\n"}


@needs_git
def test_clone_keeps_symlinks_submodules_large_files_and_skip_dirs_in_git_only(ws, fake_remote, monkeypatch):
    monkeypatch.setattr(repositories, "FILE_CAP", 1000)
    head = fake_remote.create(GH, KEPT)
    report = sync(T(GH, "api"))
    assert folder_files(ws, "api") == {"README.md": b"hi\n", "pkg/mod.py": b"m\n"}
    assert not (ws / "api" / "sub").exists() and not os.path.lexists(ws / "api" / "link")
    assert report.kept_in_git == 6
    assert report.note() == (f"Cloned github.com/octo/api main@{head[:7]} into api/: 2 files · 6 paths kept in Git only "
                             "(symlinks, submodules, files over 20 MB, node_modules/.venv/__pycache__)")


@needs_git
def test_repository_files_named_like_a_record_are_content(ws, fake_remote):
    head = fake_remote.create(GH, {"README.md": "x", "api.git-checkout.json": "{}",
                                           "sub/x.git-checkout.json": "{}", ".collimate-repository.json": "{}"})
    sync(T(GH, "api"))
    assert folder_files(ws, "api") == {"README.md": b"x", "api.git-checkout.json": b"{}",
                                       "sub/x.git-checkout.json": b"{}", ".collimate-repository.json": b"{}"}
    assert repositories.read_marker(ws, "api") == repositories.Marker(GH, "main", head)


@needs_git
def test_record_lives_beside_the_folder_at_the_cell_root(ws, fake_remote):
    head = fake_remote.create(GH, BASE)
    sync(T(GH, "api"))
    assert (ws / "api.git-checkout.json").is_file()
    assert not list((ws / "api").rglob("*.git-checkout.json"))
    assert not list((ws / "api").rglob(".collimate-repository.json"))
    assert folder_files(ws, "api") == fake_remote.tree(GH, head)


@needs_git
def test_orphan_record_is_replaced_by_a_fresh_clone(ws, fake_remote):
    write_marker(ws, "api", repositories.Marker("https://github.com/octo/other.git", "main", "a" * 40).data(), make_folder=False)
    head = fake_remote.create(GH, BASE)
    report = sync(T(GH, "api"))
    assert report.kind == "cloned"
    assert marker(ws) == repositories.Marker(GH, "main", head).data()


@needs_git
@pytest.mark.parametrize("failure", ["partial", "stop", "unrecorded"])
def test_reclone_over_a_left_over_record_never_leaves_it_beside_the_new_folder(
        cell, ws, fake_remote, monkeypatch, failure):
    """Deleting a cloned folder leaves its record. A clone into that name that
    lands only part of its files (C4), is stopped after they landed, or
    cannot write its record (C6) leaves the folder without a record, so a
    Push refuses (F2) instead of sending the missing files, or the remote's newer
    changes, back as edits."""
    fake_remote.create(GH, BASE)
    sync(T(GH, "api"))
    shutil.rmtree(ws / "api")
    head = fake_remote.commit(GH, {"src/b.py": "b = 2\n"})
    record = repositories.marker_path("api")
    if failure == "partial":
        _commit_then(monkeypatch, ("after", gen.PartialRefusal("refused", paths={"api/src/a.py"})))
        expected = repositories.PartialSave
    elif failure == "stop":
        _commit_then(monkeypatch, ("after", asyncio.CancelledError()))
        expected = asyncio.CancelledError
    else:
        _commit_then(monkeypatch, None, ("instead", RuntimeError("host down")))
        expected = repositories.RepositoryError
    with pytest.raises(expected):
        sync(T(GH, "api"))
    assert (ws / "api").is_dir() and record not in cell.read_cell_files()
    if failure == "partial":
        (ws / "api/src/a.py").unlink()                # what the cell kept
    if failure == "unrecorded":
        (ws / record).unlink()                        # staged, never committed
    assert not (ws / record).exists()
    monkeypatch.setattr(repositories, "_commit", lambda: gen.commit())
    with pytest.raises(repositories.RepositoryError) as refused:
        push(T(GH, "api"))
    assert str(refused.value) == repositories.F2.format(f="api")
    assert fake_remote.head(GH) == head


@needs_git
def test_record_left_by_a_renamed_folder_never_adopts_a_new_folder(ws, fake_remote):
    """A record stays at the root when its folder is renamed; a new folder of
    that name holding none of the recorded commit's files is refused (F9) by
    Push and Clone / Update instead of replacing every file on the remote."""
    head = fake_remote.create(GH, BASE)
    sync(T(GH, "api"))
    (ws / "api").rename(ws / "web")
    (ws / "api").mkdir()
    (ws / "api/server.py").write_text("print(1)\n")
    target = repositories.resolve_target(ws, GH)
    assert target == T(GH, "api")
    expected = repositories.F9.format(f="api", r=D, B7=head[:7])
    with pytest.raises(repositories.RepositoryError) as refused:
        push(target)
    assert str(refused.value) == expected
    assert fake_remote.head(GH) == head
    fake_remote.commit(GH, {"README.md": "moved\n"})
    with pytest.raises(repositories.RepositoryError) as refused:
        sync(target)
    assert str(refused.value) == expected
    assert folder_files(ws, "api") == {"server.py": b"print(1)\n"}
    assert marker(ws)["commit"] == head


def test_record_path_that_is_a_directory_refuses_f3_before_any_git_call(ws, git_spy):
    (ws / "api.git-checkout.json").mkdir()
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(GH, "api"))
    assert str(failure.value) == repositories.F3.format(f="api", reason="not a regular file")
    assert git_spy.calls == []
    assert not (ws / "api").exists()


def test_resolve_target_ignores_an_orphan_record(tmp_path):
    write_marker(tmp_path, "custom", VALID, make_folder=False)
    lines: list = []
    assert repositories.resolve_target(tmp_path, GH, log=lines.append) == T(GH, "api")
    assert lines == []


def test_folder_named_like_a_record_is_refused():
    for name in ("x.git-checkout.json", "X.Git-Checkout.JSON"):
        with pytest.raises(ValueError) as failure:
            repositories.check_folder(name)
        assert str(failure.value) == repositories.I5b


@needs_git
def test_clone_refuses_case_colliding_paths(ws, fake_remote):
    fake_remote.create(GH, {"README.md": "a", "readme.md": "b"})
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(GH, "api"))
    assert str(failure.value) == repositories.R2.format(r=D, a="README.md", b="readme.md")
    assert not (ws / "api").exists()


@needs_git
def test_clone_named_branch_missing_branch_and_empty_repository(ws, fake_remote):
    fake_remote.create(GH, BASE)
    dev = fake_remote.commit(GH, {"dev.txt": "dev\n"}, branch="dev")
    sync(T(GH, "api"), branch="dev")
    assert marker(ws) == repositories.Marker(GH, "dev", dev).data()
    assert (ws / "api" / "dev.txt").read_text() == "dev\n"
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(GH, "api2"), branch="nope")
    assert str(failure.value) == "github.com/octo/api has no branch nope. Nothing changed"
    empty = fake_remote.path("https://github.com/octo/empty.git")
    empty.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", "--bare", str(empty)], check=True, capture_output=True)
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T("https://github.com/octo/empty.git", "empty"))
    assert str(failure.value) == "github.com/octo/empty has no commits yet; there is nothing to clone"
    assert not (ws / "empty").exists() and not (ws / "api2").exists()


@needs_git
def test_anonymous_clone_of_public_repository(ws, fake_remote, git_spy):
    fake_remote.create(GH, BASE)
    sync(T(GH, "api"), account=None)
    assert git_spy.calls
    for _argv, env in git_spy.calls:
        assert "GIT_CONFIG_COUNT" not in env and "GIT_CONFIG_VALUE_0" not in env


@needs_git
def test_private_without_token_names_the_account_fix(ws, fake_remote):
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T("https://github.com/octo/secret.git", "secret"), account=None)
    assert str(failure.value) == repositories.N2.format(r="github.com/octo/secret")
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T("https://github.com/octo/secret.git", "secret"), account=ACCOUNT)
    assert str(failure.value) == repositories.N3.format(r="github.com/octo/secret", cred="token", hint3=" (a fine-grained token must list the repository)")
    assert TOKEN not in str(failure.value)


# ── update ────────────────────────────────────────────────────────────


@needs_git
def test_current_head_needs_only_ls_remote(cell, ws, fake_remote, git_spy):
    target = cloned(fake_remote, ws)
    head = marker(ws)["commit"]
    git_spy.calls.clear()
    updates = len(cell.updates)
    report = sync(target)
    assert spawned(git_spy) == ["ls-remote"]
    assert report.kind == "current" and len(cell.updates) == updates
    assert report.note() == f"Checked github.com/octo/api main: api/ is up to date at {head[:7]}"


@needs_git
def test_update_fast_forward_without_local_edits(ws, fake_remote):
    target = cloned(fake_remote, ws)
    before = marker(ws)["commit"]
    head = fake_remote.commit(GH, {"README.md": "new\n", "src/b.py": None, "new/n.txt": "n\n"})
    logs: list = []
    report = sync(target, logs=logs)
    assert folder_files(ws, "api") == fake_remote.tree(GH, head)
    assert marker(ws)["commit"] == head
    assert (report.kind, report.written, report.deleted, report.local_edits, report.commits) == ("updated", 2, 1, 0, 1)
    assert report.note() == (f"Updated api/ from github.com/octo/api main {before[:7]}→{head[:7]} (1 new commits): "
                             "2 files written, 1 deleted · no local edits")
    assert not (ws / "api" / "src" / "b.py").exists()


@needs_git
def test_update_merges_keeping_local_edits_additions_and_deletions(ws, fake_remote):
    target = cloned(fake_remote, ws)
    (ws / "api/src/a.py").write_text("a = 2  # mine\n")
    (ws / "api/mine.txt").write_text("mine\n")
    (ws / "api/src/b.py").unlink()
    head = fake_remote.commit(GH, {"README.md": "upstream\n", "docs/c.txt": "c2\n"})
    report = sync(target)
    files = folder_files(ws, "api")
    assert files == {"README.md": b"upstream\n", "src/a.py": b"a = 2  # mine\n", "mine.txt": b"mine\n",
                     "docs/c.txt": b"c2\n"}
    assert marker(ws)["commit"] == head          # never L or M
    assert (report.written, report.deleted, report.local_edits) == (2, 0, 3)
    assert report.note().endswith("2 files written, 0 deleted · kept your edits in 3 files")


@needs_git
def test_update_conflict_changes_nothing_and_lists_paths(cell, ws, fake_remote):
    target = cloned(fake_remote, ws)
    (ws / "api/src/a.py").write_text("mine\n")
    (ws / "api/src/b.py").write_text("mine too\n")
    (ws / "api/n.txt").write_text("my n\n")
    fake_remote.commit(GH, {"src/a.py": "theirs\n", "src/b.py": None, "n.txt": "their n\n",
                                    "README.md": "ok\n"})
    before, updates = snapshot(ws), len(cell.updates)
    with pytest.raises(repositories.RepositoryConflict) as failure:
        sync(target)
    exc = failure.value
    assert str(exc) == ("Did not update github.com/octo/api: 3 files conflict between your edits and the remote's main "
                        "(n.txt, src/a.py, src/b.py). Nothing changed")
    assert exc.details == ("Conflict: github.com/octo/api n.txt", "Conflict: github.com/octo/api src/a.py", "Conflict: github.com/octo/api src/b.py")
    assert snapshot(ws) == before and len(cell.updates) == updates


@needs_git
def test_upstream_add_over_ignored_cell_file_is_a_conflict(ws, fake_remote):
    target = cloned(fake_remote, ws, files={**BASE, ".gitignore": "*.log\n"})
    (ws / "api/debug.log").write_text("local log\n")
    fake_remote.commit(GH, {"debug.log": "tracked now\n"})
    before = snapshot(ws)
    with pytest.raises(repositories.RepositoryConflict) as failure:
        sync(target)
    assert failure.value.details == ("Conflict: github.com/octo/api debug.log (an ignored file in the cell; the remote now tracks it)",)
    assert snapshot(ws) == before


@needs_git
def test_upstream_add_under_ignored_cell_folder_is_a_conflict(ws, fake_remote):
    target = cloned(fake_remote, ws, files={**BASE, ".gitignore": "build/\ncache\n"})
    (ws / "api/build").mkdir()
    (ws / "api/build/out.txt").write_text("local build\n")
    (ws / "api/cache").write_text("a file the cell ignores\n")
    fake_remote.commit(GH, {"build/out.txt": "forced in\n", "cache/x": "now a folder\n"})
    with pytest.raises(repositories.RepositoryConflict) as failure:
        sync(target)
    assert failure.value.details == (
        "Conflict: github.com/octo/api build/out.txt (an ignored file in the cell; the remote now tracks it)",
        "Conflict: github.com/octo/api cache (an ignored file in the cell; the remote now tracks it)")


@needs_git
def test_untracked_ignored_cell_files_stay_and_are_never_committed(ws, fake_remote):
    target = cloned(fake_remote, ws, files={**BASE, ".gitignore": "*.log\n"})
    (ws / "api/run.log").write_text("local\n")
    head = fake_remote.commit(GH, {"README.md": "up\n"})
    report = sync(target)
    assert report.local_edits == 0
    assert (ws / "api/run.log").read_text() == "local\n"
    (ws / "api/src/a.py").write_text("edited\n")
    pushed = push(target)
    assert pushed.kind == "pushed"
    tree = fake_remote.tree(GH, fake_remote.head(GH))
    assert "run.log" not in tree and tree["src/a.py"] == b"edited\n"
    assert fake_remote.commit_info(GH, "main")["parents"] == [head]


@needs_git
def test_rewritten_history_stops(ws, fake_remote):
    target = cloned(fake_remote, ws)
    fake_remote.commit(GH, {"README.md": "two\n"})
    sync(target)
    recorded = marker(ws)["commit"]
    fake_remote.rewrite(GH)
    before = snapshot(ws)
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(target)
    assert str(failure.value) == repositories.R6.format(b="main", r=D, B7=recorded[:7], f="api")
    assert snapshot(ws) == before


@needs_git
def test_deleted_branch_stops(ws, fake_remote):
    fake_remote.create(GH, BASE)
    fake_remote.commit(GH, {"dev.txt": "d"}, branch="dev")
    target = T(GH, "api")
    sync(target, branch="dev")
    fake_remote.delete_branch(GH, "dev")
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(target)
    assert str(failure.value) == ("Branch dev no longer exists in github.com/octo/api. Nothing changed; rename api/ and "
                                  "clone again to start over")


@needs_git
def test_unknown_recorded_commit_needs_no_fetch_by_sha(ws, fake_remote, git_spy):
    target = cloned(fake_remote, ws)
    data = marker(ws)
    write_marker(ws, "api", {**data, "commit": "b" * 40})
    fake_remote.commit(GH, {"README.md": "moved\n"})
    with pytest.raises(repositories.RepositoryError, match="no longer contains the recorded commit bbbbbbb"):
        sync(target)
    [env] = [env for argv, env in git_spy.calls if subcommand_of(argv) == "merge-base"]
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    write_marker(ws, "api", data)
    git_spy.fail("merge-base", code=1)
    with pytest.raises(repositories.RepositoryError, match="no longer contains the recorded commit"):
        sync(target)


@needs_git
def test_file_to_folder_and_folder_to_file_transitions(ws, fake_remote):
    target = cloned(fake_remote, ws, files={"a": "file a\n", "d/x": "in d\n", "keep.txt": "k\n"})
    head = fake_remote.commit(GH, {"a": None, "a/b": "now under a\n", "d/x": None, "d": "now a file\n"})
    sync(target)
    assert folder_files(ws, "api") == {"a/b": b"now under a\n", "d": b"now a file\n", "keep.txt": b"k\n"}
    assert marker(ws)["commit"] == head


@needs_git
def test_file_becoming_symlink_or_large_is_removed_from_cell(ws, fake_remote, monkeypatch):
    monkeypatch.setattr(repositories, "FILE_CAP", 1000)
    target = cloned(fake_remote, ws, files={"README.md": "r\n", "s.txt": "s\n", "big.txt": "small\n"})
    fake_remote.commit(GH, {"s.txt": ("link", "README.md"), "big.txt": b"x" * 2000})
    logs: list = []
    report = sync(target, logs=logs)
    assert folder_files(ws, "api") == {"README.md": b"r\n"}
    assert "api/s.txt became a symlink in the repository and is now kept in Git only" in logs
    assert "api/big.txt became a file over 20 MB in the repository and is now kept in Git only" in logs
    assert report.deleted == 2 and report.kept_in_git == 2


@needs_git
def test_merge_pushing_an_edited_file_over_the_cap_refuses_and_keeps_the_edit(ws, fake_remote, monkeypatch):
    monkeypatch.setattr(repositories, "FILE_CAP", 1000)
    lines = "".join(f"line {i:03d}\n" for i in range(50))                     # 450 bytes
    target = cloned(fake_remote, ws, files={"README.md": "r\n", "f.txt": lines})
    base = marker(ws)
    edited = "E" * 250 + "\n" + lines.split("\n", 1)[1]                        # the edit alone stays under the cap
    (ws / "api/f.txt").write_text(edited)
    head = fake_remote.commit(GH, {"f.txt": lines + "y" * 400 + "\n"})  # the remote's alone stays under it too
    refusal = repositories.R8.format(f="api", p="f.txt")
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(target)
    assert str(failure.value) == refusal
    assert marker(ws) == base and folder_files(ws, "api") == {"README.md": b"r\n", "f.txt": edited.encode()}
    with pytest.raises(repositories.RepositoryError) as failure:
        push(target)
    assert str(failure.value) == refusal
    assert fake_remote.head(GH) == head and marker(ws) == base
    assert folder_files(ws, "api")["f.txt"] == edited.encode()


@needs_git
def test_cell_file_inside_submodule_path_is_refused(ws, fake_remote):
    target = cloned(fake_remote, ws, files={"README.md": "r\n", "sub": ("gitlink", "a" * 40)})
    (ws / "api/sub").mkdir()
    (ws / "api/sub/x.txt").write_text("mine\n")
    fake_remote.commit(GH, {"README.md": "moved\n"})
    before = snapshot(ws)
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(target)
    assert str(failure.value) == repositories.R7.format(f="api", p="sub/x.txt", kind="submodule", k="sub")
    assert snapshot(ws) == before


@needs_git
def test_cell_file_under_repository_symlink_dir_is_refused_and_nothing_is_written_outside(
        ws, fake_remote, private_tmp):
    target = cloned(fake_remote, ws, files={"README.md": "r\n", "link": ("link", "..")})
    (ws / "api/link").mkdir()
    (ws / "api/link/x").write_text("would escape\n")
    fake_remote.commit(GH, {"README.md": "moved\n"})
    seen = []
    real = repositories._write_cell_into_checkout

    def watch(workspace, target, checkout, before, cell):
        seen.append(checkout.parent)
        return real(workspace, target, checkout, before, cell)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(repositories, "_write_cell_into_checkout", watch)
        with pytest.raises(repositories.RepositoryError) as failure:
            sync(target)
    assert str(failure.value) == repositories.R7.format(f="api", p="link/x", kind="symlink", k="link")
    assert seen == []
    assert not any(p.name == "x" for p in private_tmp.rglob("*"))
    with pytest.raises(repositories.RepositoryError, match="cannot be synced"):
        push(target)


@needs_git
def test_overlay_never_deletes_kept_paths(ws, fake_remote, monkeypatch):
    monkeypatch.setattr(repositories, "FILE_CAP", 1000)
    target = cloned(fake_remote, ws, files=KEPT)
    (ws / "api/README.md").write_text("edited\n")
    push(target, message="Edit")
    out = fake_remote.git(GH, "ls-tree", "-r", "--name-only", "main").decode().split()
    assert set(out) == set(KEPT)


@needs_git
def test_crlf_eol_attribute_and_executable_files_show_no_spurious_edits(ws, fake_remote):
    files = {".gitattributes": "*.txt text eol=crlf\n", "a.txt": "line1\nline2\n",
             "run.sh": ("exec", b"#!/bin/sh\necho hi\n"), "README.md": "r\n"}
    target = cloned(fake_remote, ws, files=files)
    assert (ws / "api/a.txt").read_bytes() == b"line1\r\nline2\r\n"
    assert push(target).kind == "nothing"
    fake_remote.commit(GH, {"README.md": "moved\n"})
    report = sync(target)
    assert report.local_edits == 0 and report.note().endswith("· no local edits")
    modes = fake_remote.git(GH, "ls-tree", "main", "run.sh").decode()
    assert modes.startswith("100755")


@needs_git
def test_branch_setting_mismatch_refuses(ws, fake_remote):
    target = cloned(fake_remote, ws)
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(target, branch="dev")
    assert str(failure.value) == repositories.F5.format(f="api", mb="main", b="dev")
    assert sync(target, branch="main").kind == "current"


@needs_git
def test_partial_clone_falls_back_when_server_lacks_filter(ws, fake_remote):
    fake_remote.create(GH, BASE)
    fake_remote.git(GH, "config", "uploadpack.allowFilter", "false")
    report = sync(T(GH, "api"))
    assert report.kind == "cloned" and folder_files(ws, "api") == {p: d.encode() for p, d in BASE.items()}
    head = fake_remote.commit(GH, {"README.md": "moved\n"})
    (ws / "api/src/a.py").write_text("mine\n")
    assert sync(T(GH, "api")).after == head


# ── two-phase commit ──────────────────────────────────────────────────


def _commit_then(monkeypatch, *failures):
    """``repositories._commit`` whose n-th call does ``failures[n]``: None
    commits; ("after", exc) commits and then raises; ("instead", exc) raises
    without committing."""
    plan = list(failures)
    calls: list = []

    async def commit():
        step = plan.pop(0) if plan else None
        calls.append(step)
        if step is None:
            await gen.commit()
            return
        when, exc = step
        if when == "after":
            await gen.commit()
        raise exc

    monkeypatch.setattr(repositories, "_commit", commit)
    return calls


@needs_git
def test_partial_refusal_on_update_keeps_old_marker_and_converges(ws, fake_remote, monkeypatch):
    target = cloned(fake_remote, ws)
    base = marker(ws)
    (ws / "api/mine.txt").write_text("mine\n")
    head = fake_remote.commit(GH, {"src/a.py": "a = 2\n", "src/b.py": "b = 2\n"})
    _commit_then(monkeypatch, ("after", gen.PartialRefusal("refused", paths={"api/src/a.py"})))
    with pytest.raises(repositories.PartialSave) as failure:
        sync(target)
    assert failure.value.refused == ("api/src/a.py",)
    assert str(failure.value) == ("Updated only part of api/: the cell refused api/src/a.py (refused by the cell). "
                                  "api.git-checkout.json keeps the previous commit, so the next Git Clone / "
                                  "Update retries them; your edits are kept")
    assert marker(ws) == base
    (ws / "api/src/a.py").write_text("a = 1\n")      # what the cell kept
    monkeypatch.setattr(repositories, "_commit", lambda: gen.commit())
    report = sync(target)
    assert report.kind == "updated" and marker(ws)["commit"] == head
    assert folder_files(ws, "api") == {**fake_remote.tree(GH, head), "mine.txt": b"mine\n"}


@needs_git
def test_partial_refusal_on_clone_leaves_no_marker_and_names_paths(cell, ws, fake_remote, monkeypatch):
    fake_remote.create(GH, BASE)
    calls = _commit_then(monkeypatch, ("after", gen.PartialRefusal("refused", paths={"api/src/a.py"})))
    with pytest.raises(repositories.PartialSave) as failure:
        sync(T(GH, "api"))
    assert str(failure.value) == ("Cloned only part of github.com/octo/api into api/: the cell refused api/src/a.py "
                                  "(refused by the cell), and api/ has no checkout record yet; delete api/ before "
                                  "cloning again")
    assert not (ws / repositories.marker_path("api")).exists()
    assert len(calls) == 1 and len(cell.updates) == 1


@needs_git
def test_refusal_outside_repository_folders_still_commits_marker(ws, fake_remote, monkeypatch):
    fake_remote.create(GH, BASE)
    _commit_then(monkeypatch, ("after", gen.PartialRefusal("refused", paths={"notes/other.txt"})))
    logs: list = []
    report = sync(T(GH, "api"), logs=logs)
    assert report.kind == "cloned" and marker(ws)["commit"] == report.after
    assert "The cell refused files outside the repository folders: notes/other.txt" in logs


@needs_git
def test_partial_refusal_names_the_hosts_reason(ws, fake_remote, monkeypatch):
    fake_remote.create(GH, BASE)
    refusal = gen.PartialRefusal("refused", paths={"api/README.md"},
                                 refused=[{"path": "api/README.md", "error": "locked by run x"}])
    _commit_then(monkeypatch, ("after", refusal))
    with pytest.raises(repositories.PartialSave, match=r"refused api/README.md \(locked by run x\)"):
        sync(T(GH, "api"))


@needs_git
def test_marker_commit_failure_converges_on_rerun(ws, fake_remote, monkeypatch):
    target = cloned(fake_remote, ws)
    base = marker(ws)
    head = fake_remote.commit(GH, {"README.md": "moved\n"})
    _commit_then(monkeypatch, None, ("instead", RuntimeError("host down")))
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(target)
    assert str(failure.value) == ("Updated api/ but could not record the new commit in api.git-checkout.json "
                                  "(RuntimeError); the next Git Clone / Update finishes it")
    write_marker(ws, "api", base)                     # the cell still records the old commit
    monkeypatch.setattr(repositories, "_commit", lambda: gen.commit())
    report = sync(target)
    # the files that landed under the old marker read as edits identical to the remote's
    assert (report.kind, report.written, report.deleted) == ("updated", 0, 0)
    assert marker(ws)["commit"] == head and folder_files(ws, "api") == fake_remote.tree(GH, head)


@needs_git
def test_clone_marker_commit_failure_is_c6(ws, fake_remote, monkeypatch):
    fake_remote.create(GH, BASE)
    _commit_then(monkeypatch, None, ("instead", RuntimeError("host down")))
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(GH, "api"))
    assert str(failure.value) == ("Cloned github.com/octo/api into api/ but could not write api.git-checkout.json "
                                  "(RuntimeError); delete api/ and clone again")


def _record_refusal(error):
    return gen.PartialRefusal("refused", paths=["api.git-checkout.json"],
                              refused=[{"path": "api.git-checkout.json", "reason": "handler_invalid",
                                        "error": error}])


@needs_git
def test_record_commit_retries_a_verification_race(cell, ws, fake_remote, monkeypatch):
    head = fake_remote.create(GH, BASE)
    calls = _commit_then(monkeypatch, None, ("instead", _record_refusal(repositories._VERIFY_RACED)))
    report = sync(T(GH, "api"))
    assert report.kind == "cloned"
    assert marker(ws) == repositories.Marker(GH, "main", head).data()
    assert len(calls) == 3                            # the files, then two record commits
    assert [sorted(u["files"]) for u in cell.updates] == [sorted(f"api/{p}" for p in BASE),
                                                          ["api.git-checkout.json"]]


@needs_git
def test_persistent_verification_race_reports_c6_after_three_attempts(ws, fake_remote, monkeypatch):
    fake_remote.create(GH, BASE)
    race = ("instead", _record_refusal(repositories._VERIFY_RACED))
    calls = _commit_then(monkeypatch, None, race, race, race, race)
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(GH, "api"))
    assert str(failure.value) == repositories.C6.format(r=D, f="api",
                                                        reason="cell changed during verification; retry")
    assert len(calls) == 1 + repositories._RECORD_ATTEMPTS


@needs_git
def test_other_record_refusal_is_not_retried(ws, fake_remote, monkeypatch):
    fake_remote.create(GH, BASE)
    calls = _commit_then(monkeypatch, None,
                         ("instead", _record_refusal("Not a valid Git checkout record (bad commit)")))
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(GH, "api"))
    assert str(failure.value) == repositories.C6.format(
        r=D, f="api", reason="Not a valid Git checkout record (bad commit)")
    assert len(calls) == 2


@needs_git
def test_file_conflict_error_reports_c1(cell, ws, fake_remote, monkeypatch):
    fake_remote.create(GH, BASE)
    _commit_then(monkeypatch, ("instead", gen.FileConflictError({})))
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(GH, "api"))
    assert str(failure.value) == repositories.C1
    assert not (ws / repositories.marker_path("api")).exists()
    assert cell.updates == []


@needs_git
def test_other_commit_failure_reports_c2(ws, fake_remote, monkeypatch):
    fake_remote.create(GH, BASE)
    _commit_then(monkeypatch, ("instead", gen.IPCError("closed")))
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(GH, "api"))
    assert str(failure.value) == "Could not save api/ to the cell (IPCError); nothing was saved. Run it again"


@needs_git
def test_half_applied_update_converges_on_rerun(ws, fake_remote, monkeypatch):
    target = cloned(fake_remote, ws)
    base = marker(ws)
    head = fake_remote.commit(GH, {"README.md": "r2\n", "src/a.py": "a2\n", "src/b.py": "b2\n"})
    real = gen.update_file
    written: list = []

    def flaky(path, data, **kw):
        if len(written) == 1:
            raise OSError("disk full")
        written.append(path)
        return real(path, data, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gen, "update_file", flaky)
        with pytest.raises(repositories.RepositoryError) as failure:
            sync(target)
    assert str(failure.value) == "Could not save api/ to the cell (OSError); nothing was saved. Run it again"
    assert len(written) == 1 and marker(ws) == base
    report = sync(target)
    assert report.kind == "updated" and marker(ws)["commit"] == head
    assert folder_files(ws, "api") == fake_remote.tree(GH, head)


@needs_git
def test_apply_runs_off_loop_and_cancellation_joins_it(ws, fake_remote, monkeypatch):
    fake_remote.create(GH, BASE)
    entered, release = threading.Event(), threading.Event()
    observed: dict = {}
    real = repositories._apply

    def apply(workspace, changes):
        observed["thread"] = threading.get_ident()
        observed["checkout"] = changes[0].checkout
        entered.set()
        release.wait(5)
        observed["exists_on_resume"] = changes[0].checkout.exists()
        real(workspace, changes)

    monkeypatch.setattr(repositories, "_apply", apply)

    async def flow():
        task = asyncio.create_task(repositories.sync(T(GH, "api"), account=None, log=_drop))
        assert await asyncio.to_thread(entered.wait, 5)
        assert observed["thread"] != threading.get_ident()
        task.cancel()
        try:
            await asyncio.sleep(0.05)
            assert not task.done(), "cancellation must join the placement worker"
            assert observed["checkout"].exists()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(flow())
    assert observed["exists_on_resume"]
    assert not observed["checkout"].exists()
    assert (ws / "api/README.md").read_text() == "hello\n"
    assert not (ws / repositories.marker_path("api")).exists()


# ── push ──────────────────────────────────────────────────────────────


@needs_git
def test_push_fast_forward_one_commit_parent_is_recorded(cell, ws, fake_remote):
    target = cloned(fake_remote, ws)
    base = marker(ws)["commit"]
    (ws / "api/README.md").write_text("edited\n")
    logs: list = []
    report = push(target, message="Edit the readme\n\nWith a body", logs=logs)
    head = fake_remote.head(GH)
    info = fake_remote.commit_info(GH, head)
    assert info == {"parents": [base], "author": "Ada Lovelace <ada@example.org>",
                    "message": "Edit the readme\n\nWith a body"}
    assert marker(ws) == repositories.Marker(GH, "main", head).data()
    assert (report.kind, report.pushed, report.recorded, report.pushed_to) == ("pushed", head, head, "main")
    assert report.note() == f"Pushed api/ to github.com/octo/api main {base[:7]}→{head[:7]}: 1 files changed"
    assert f"Pushing {head[:7]} to github.com/octo/api main as Ada Lovelace <ada@example.org>" in logs
    assert sorted(cell.updates[-1]["files"]) == ["api.git-checkout.json"]


@needs_git
@pytest.mark.parametrize("edit, message", [
    (["README.md"], "Update README.md"),
    (["README.md", "src/a.py"], "Update README.md and src/a.py"),
    (["README.md", "docs/c.txt", "src/a.py"], "Update README.md, docs/c.txt and src/a.py"),
    (["README.md", "docs/c.txt", "src/a.py", "src/b.py"], "Update 4 files in api"),
])
def test_push_generated_message_names_files(ws, fake_remote, edit, message):
    target = cloned(fake_remote, ws)
    for path in edit:
        (ws / "api" / path).write_text("changed\n")
    push(target, message=None)
    assert fake_remote.commit_info(GH, "main")["message"] == message


@needs_git
def test_push_merges_moved_branch_pushes_and_writes_github_files(ws, fake_remote):
    target = cloned(fake_remote, ws)
    (ws / "api/src/a.py").write_text("mine\n")
    upstream = fake_remote.commit(GH, {"docs/c.txt": "theirs\n"})
    report = push(target, message="Mine")
    head = fake_remote.head(GH)
    parents = fake_remote.commit_info(GH, head)["parents"]
    assert len(parents) == 2 and parents[1] == upstream
    assert fake_remote.commit_info(GH, parents[0])["message"] == "Mine"
    assert (ws / "api/docs/c.txt").read_text() == "theirs\n"
    assert marker(ws)["commit"] == head
    assert report.kind == "merged" and report.written == 1
    assert report.note() == (f"Pushed api/ to github.com/octo/api main at {head[:7]} after merging 1 new commits from the remote: "
                             "1 files of yours, 1 files from the remote written to api/")


@needs_git
def test_push_conflict_pushes_nothing(ws, fake_remote):
    target = cloned(fake_remote, ws)
    (ws / "api/src/a.py").write_text("mine\n")
    upstream = fake_remote.commit(GH, {"src/a.py": "theirs\n"})
    before = snapshot(ws)
    with pytest.raises(repositories.RepositoryConflict) as failure:
        push(target)
    assert str(failure.value) == ("Did not push github.com/octo/api: the remote's main moved and 1 files conflict with your edits "
                                  "(src/a.py). Nothing was pushed or changed; push to a new branch instead "
                                  "(Push to branch)")
    assert fake_remote.head(GH) == upstream and snapshot(ws) == before


@needs_git
def test_push_new_branch_switches_folder_and_refuses_existing_branch(ws, fake_remote):
    target = cloned(fake_remote, ws)
    base = marker(ws)["commit"]
    (ws / "api/src/a.py").write_text("mine\n")
    fake_remote.commit(GH, {"src/a.py": "theirs\n"})
    main = fake_remote.head(GH)
    report = push(target, push_branch="feature/x")
    feature = fake_remote.head(GH, "feature/x")
    assert fake_remote.head(GH) == main
    assert fake_remote.commit_info(GH, feature)["parents"] == [base]
    assert marker(ws) == repositories.Marker(GH, "feature/x", feature).data()
    assert report.compare_url == "https://github.com/octo/api/compare/main...feature/x?expand=1"
    assert report.note() == (f"Pushed api/ to new branch feature/x of github.com/octo/api at {feature[:7]}: 1 files changed; "
                             "api/ now follows feature/x · open a merge or pull request: "
                             "https://github.com/octo/api/compare/main...feature/x?expand=1")
    (ws / "api/README.md").write_text("again\n")
    with pytest.raises(repositories.RepositoryError) as failure:
        push(target, push_branch="main")
    assert str(failure.value) == ("Branch main already exists in github.com/octo/api; pushing never overwrites a branch, so "
                                  "choose a new name. Nothing changed")
    with pytest.raises(ValueError) as failure:
        push(target, push_branch="a..b")
    assert str(failure.value) == repositories.I7
    assert push(target, push_branch="feature/x").kind == "pushed"
    (ws / "api/README.md").write_text("once more\n")
    report = push(target, push_branch="fix#12")                 # a valid branch name with a URL-special character
    assert fake_remote.head(GH, "fix#12") == report.pushed
    assert report.compare_url == "https://github.com/octo/api/compare/feature/x...fix%2312?expand=1"


@needs_git
def test_push_nothing_to_push_current_and_moved(cell, ws, fake_remote):
    target = cloned(fake_remote, ws)
    base = marker(ws)["commit"]
    updates = len(cell.updates)
    report = push(target)
    assert report.kind == "nothing" and report.recorded == base
    assert report.note() == f"Nothing to push: api/ matches github.com/octo/api main at {base[:7]}"
    fake_remote.commit(GH, {"README.md": "moved\n"})
    report = push(target)
    assert report.note() == (f"Nothing to push: api/ matches github.com/octo/api main at {base[:7]} · the remote's main has 1 "
                             "newer commits; run Git Clone / Update to bring them in")
    assert len(cell.updates) == updates and marker(ws)["commit"] == base


@needs_git
def test_push_absorbed_when_github_already_has_the_edits(ws, fake_remote, git_spy):
    target = cloned(fake_remote, ws)
    (ws / "api/README.md").write_text("same edit\n")
    upstream = fake_remote.commit(GH, {"README.md": "same edit\n"})
    report = push(target)
    assert report.kind == "absorbed" and report.recorded == upstream
    assert fake_remote.head(GH) == upstream and marker(ws)["commit"] == upstream
    assert "push" not in git_spy.subcommands()
    assert report.note() == (f"Nothing new to push: the remote's main at {upstream[:7]} already has your edits; api/ "
                             "now records it (0 files from the remote written)")


@needs_git
def test_push_refuses_deleting_every_file(ws, fake_remote):
    target = cloned(fake_remote, ws)
    for path in BASE:
        (ws / "api" / path).unlink()
    with pytest.raises(repositories.RepositoryError) as failure:
        push(target)
    assert str(failure.value) == repositories.P2.format(r=D, f="api")


def _status(flag_summary: str) -> bytes:
    return f"To file:///x\n{flag_summary.split(' ', 1)[0]}\tdeadbeef:refs/heads/main\t{flag_summary.split(' ', 1)[1]}\nDone\n".encode()


@needs_git
@pytest.mark.parametrize("row, expected", [
    ("T1", "Could not confirm whether the push of {P7} to github.com/octo/api main reached github.com"),
    ("T3", "Did not push github.com/octo/api main: the branch moved on the remote while pushing"),
    ("T4", "github.com refused the push to github.com/octo/api main: a protected branch, push rule or server hook"),
    ("T4-gh013", "github.com refused the push to github.com/octo/api main: a protected branch, push rule or server hook"),
    ("T5", "The selected Git account cannot push to github.com/octo/api; give its token write access"),
    ("T6", "github.com refused the push to github.com/octo/api main. Nothing changed; push to a new branch"),
    ("T7-auth", "github.com rejected the selected Git account's token; replace it in Key manager"),
    ("T7-forbidden", "The selected Git account cannot push to github.com/octo/api; give its token write access"),
    ("T7-readonly", "The selected Git account cannot push to github.com/octo/api; give its token write access"),
    ("T7-notfound", "github.com/octo/api was not found with the selected Git account"),
    ("T7-network", "Could not reach github.com while pushing github.com/octo/api"),
    ("T7-timeout", "git timed out while pushing github.com/octo/api"),
    ("T7-other", "git failed while pushing github.com/octo/api (exit 128)"),
    ("T8", "git reported the push to github.com/octo/api main but github.com still shows {B7}"),
    ("T9", "Did not push github.com/octo/api main: the branch moved on the remote while pushing"),
])
def test_push_decision_table(ws, fake_remote, git_spy, monkeypatch, row, expected):
    target = cloned(fake_remote, ws)
    base = marker(ws)["commit"]
    (ws / "api/README.md").write_text("edited\n")
    if row == "T1":
        git_spy.before("push", lambda: git_spy.fail("ls-remote", code=128,
                                                    stderr=b"fatal: Could not resolve host: github.com"))
    elif row == "T3":
        git_spy.fail("push", code=1, stdout=_status("! [rejected] (fetch first)"))
    elif row == "T4":
        git_spy.fail("push", code=1, stdout=_status("! [remote rejected] (protected branch hook declined)"))
    elif row == "T4-gh013":
        git_spy.fail("push", code=1, stdout=_status("! [remote rejected] (GH013: Repository rule violations)"))
    elif row == "T5":
        git_spy.fail("push", code=1, stdout=_status("! [remote rejected] (permission denied)"))
    elif row == "T6":
        git_spy.fail("push", code=1, stdout=_status("! [remote rejected] (hook declined)"))
    elif row.startswith("T7"):
        stderr = {"auth": b"fatal: Authentication failed", "forbidden": b"The requested URL returned error: 403",
                  "notfound": b"remote: Repository not found.", "network": b"Could not resolve host: github.com",
                  "other": b"fatal: weird",
                  "readonly": b"ERROR: The key you are authenticating with has been marked as read only.\n"
                              b"fatal: Could not read from remote repository."}
        kind = row.split("-")[1]
        if kind == "timeout":
            original_run = git_run.Git.run

            async def run(self, args, cwd, **kw):
                if args[0] == "push":       # what Git.run raises once a push outlives its timeout
                    raise git_run.GitFailure("timeout", "push", -1)
                return await original_run(self, args, cwd, **kw)

            monkeypatch.setattr(git_run.Git, "run", run)
        else:
            git_spy.fail("push", code=128, stderr=stderr[kind])
    elif row == "T8":
        git_spy.fail("push", code=0, stdout=b"Done\n")
    elif row == "T9":
        git_spy.before("push", lambda: fake_remote.commit(GH, {"other.txt": "someone else\n"}))
    with pytest.raises(repositories.RepositoryError) as failure:
        push(target)
    pushing = next((argv[-1].split(":")[0] for argv, _ in git_spy.calls if subcommand_of(argv) == "push"), "")
    assert str(failure.value).startswith(expected.format(P7=pushing[:7], B7=base[:7]))
    assert TOKEN not in str(failure.value)
    assert marker(ws)["commit"] == base
    if row not in ("T1", "T9"):
        assert fake_remote.head(GH) == base


@needs_git
def test_push_nonzero_exit_but_ref_landed_is_success(ws, fake_remote, monkeypatch):
    target = cloned(fake_remote, ws)
    (ws / "api/README.md").write_text("edited\n")
    from tests._git_fixtures import _FakeProcess
    real = git_run._spawn

    async def spawn(*argv, **kwargs):
        proc = await real(*argv, **kwargs)
        if subcommand_of(argv) != "push":
            return proc
        out, _err = await proc.communicate()
        return _FakeProcess(1, out, b"error: failed to update some refs (but it did)")

    monkeypatch.setattr(git_run, "_spawn", spawn)
    report = push(target)
    assert report.kind == "pushed" and fake_remote.head(GH) == report.pushed == marker(ws)["commit"]


@needs_git
def test_push_never_forces(ws, fake_remote, git_spy):
    target = cloned(fake_remote, ws)
    (ws / "api/README.md").write_text("one\n")
    push(target)
    (ws / "api/src/a.py").write_text("two\n")
    fake_remote.commit(GH, {"docs/c.txt": "moved\n"})
    push(target)
    (ws / "api/src/b.py").write_text("three\n")
    push(target, push_branch="topic")
    pushes = [argv for argv, _ in git_spy.calls if subcommand_of(argv) == "push"]
    assert len(pushes) == 3
    for argv in pushes:
        assert not {"--force", "-f", "--force-with-lease", "--mirror", "--force-if-includes"} & set(argv)
        assert not any(a.startswith("--force") for a in argv)
        assert not argv[-1].startswith("+") and argv[-1].count(":") == 1


@needs_git
def test_push_respects_gitignore_and_never_pushes_skip_dir_deletions(ws, fake_remote):
    target = cloned(fake_remote, ws, files={**BASE, ".gitignore": "*.log\n", "node_modules/x.js": "x\n",
                                            "pkg/__pycache__/m.pyc": b"c"})
    (ws / "api/debug.log").write_text("local\n")
    (ws / "api/README.md").write_text("edited\n")
    (ws / "api/node_modules").mkdir()
    (ws / "api/node_modules/local.js").write_text("never synced\n")
    logs: list = []
    push(target, logs=logs)
    out = set(fake_remote.git(GH, "ls-tree", "-r", "--name-only", "main").decode().split())
    assert "debug.log" not in out and "node_modules/local.js" not in out
    assert {"node_modules/x.js", "pkg/__pycache__/m.pyc"} <= out
    assert "1 files under node_modules/.venv/__pycache__ in api/ are never synced" in logs


@needs_git
def test_push_stop_after_spawn_records_p8(cell, ws, fake_remote, git_spy):
    target = cloned(fake_remote, ws)
    base = marker(ws)["commit"]
    (ws / "api/README.md").write_text("edited\n")

    async def flow():
        task = asyncio.current_task()
        git_spy.before("push", task.cancel)
        await repositories.push(target, account=ACCOUNT, message=None, log=_drop)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(flow())
    pushing = next(argv[-1].split(":")[0] for argv, _ in git_spy.calls if subcommand_of(argv) == "push")
    assert cell.run_outcome == "fail"
    assert cell.run_note == repositories.P8.format(P7=pushing[:7], r="github.com/octo/api", dst="main", host="github.com")
    assert marker(ws)["commit"] == base


@needs_git
def test_push_landed_then_partial_refusal_reports_p7a(ws, fake_remote, monkeypatch):
    target = cloned(fake_remote, ws)
    base = marker(ws)
    (ws / "api/src/a.py").write_text("mine\n")
    fake_remote.commit(GH, {"docs/c.txt": "theirs\n"})
    _commit_then(monkeypatch, ("after", gen.PartialRefusal("refused", paths={"api/docs/c.txt"})))
    with pytest.raises(repositories.PushedNotRecorded) as failure:
        push(target)
    head = fake_remote.head(GH)
    assert failure.value.pushed == head
    assert str(failure.value) == (f"Pushed github.com/octo/api main {head[:7]}, but the cell refused api/docs/c.txt (refused by "
                                  "the cell), so api.git-checkout.json keeps the previous commit; run Git "
                                  "Clone / Update to re-sync api/. Your edits are on github.com")
    assert marker(ws) == base


@needs_git
def test_push_landed_then_commit_failure_reports_p7b(ws, fake_remote, monkeypatch):
    target = cloned(fake_remote, ws)
    base = marker(ws)
    (ws / "api/README.md").write_text("edited\n")
    _commit_then(monkeypatch, ("instead", RuntimeError("host down")))
    with pytest.raises(repositories.PushedNotRecorded) as failure:
        push(target)
    head = fake_remote.head(GH)
    assert str(failure.value) == (f"Pushed github.com/octo/api main {head[:7]}, but the cell could not record it (RuntimeError); "
                                  "run Git Clone / Update to re-sync api/. Your edits are on github.com")
    write_marker(ws, "api", base)
    monkeypatch.setattr(repositories, "_commit", lambda: gen.commit())
    report = sync(target)
    assert (report.kind, report.written, report.deleted) == ("updated", 0, 0)
    assert marker(ws)["commit"] == head and folder_files(ws, "api") == fake_remote.tree(GH, head)
    assert push(target).kind == "nothing"


@needs_git
@pytest.mark.parametrize("landed", [True, False])
def test_push_stop_during_marker_commit_reports_p7c_only_when_the_marker_did_not_land(
        cell, ws, fake_remote, monkeypatch, landed):
    target = cloned(fake_remote, ws)
    (ws / "api/README.md").write_text("edited\n")
    stop = asyncio.CancelledError()
    stop._flush_landed = landed                     # what gen.commit says of the flush a Stop interrupted
    calls = _commit_then(monkeypatch, ("after" if landed else "instead", stop))   # a fast-forward stages no files
    with pytest.raises(asyncio.CancelledError):
        push(target)
    assert len(calls) == 1
    head = fake_remote.head(GH)
    if landed:
        assert marker(ws)["commit"] == head
        assert cell.run_note is None and cell.run_outcome is None
    else:
        assert cell.run_outcome == "fail"
        assert cell.run_note == repositories.P7c.format(r=D, dst="main", P7=head[:7], f="api", host="github.com")


@needs_git
def test_marker_branch_is_user_editable_but_branch_setting_pins_it(ws, fake_remote):
    target = cloned(fake_remote, ws)
    data = marker(ws)
    fake_remote.commit(GH, {}, branch="dev", message="branch point")
    dev = fake_remote.head(GH, "dev")
    write_marker(ws, "api", {**data, "branch": "dev", "commit": dev})
    (ws / "api/README.md").write_text("edited\n")
    logs: list = []
    report = push(target, logs=logs)
    assert report.pushed_to == "dev" and fake_remote.head(GH, "dev") == report.pushed
    assert fake_remote.head(GH) == data["commit"]
    assert "github.com/octo/api dev" in report.note()
    assert any(line.startswith("Pushing ") and "github.com/octo/api dev" in line for line in logs)
    with pytest.raises(repositories.RepositoryError) as failure:
        push(target, branch="main")
    assert str(failure.value) == repositories.F5.format(f="api", mb="dev", b="main")


# ── secrets ───────────────────────────────────────────────────────────


@needs_git
@pytest.mark.parametrize("auth", ["api_key", "basic", "ssh_key"])
def test_secret_never_on_argv_env_files_logs_notes_or_snapshot(request, ws, git_spy, private_tmp, monkeypatch, auth):
    snapshot_ = {"api_key": git_account_snapshot("github", "api_key", secrets={"api_key": TOKEN}),
                 "basic": git_account_snapshot("git", "basic", secrets={"password": "pw/s3cret+x"}),
                 "ssh_key": git_account_snapshot("github", "ssh_key")}[auth]
    bind_account(monkeypatch, snapshot_)
    account = git_account.git_account()
    remote = "https://git.example.org/octo/api.git" if auth == "basic" else GH
    hub = request.getfixturevalue("fake_ssh").hub if auth == "ssh_key" else request.getfixturevalue("fake_remote")
    forms = account.secret_forms()
    assert forms
    hub.create(remote, BASE)
    target = T(remote, "api")
    logs: list = []
    cloned_report = sync(target, account=account, logs=logs)
    (ws / "api/README.md").write_text("edited\n")
    hub.commit(remote, {"docs/c.txt": "moved\n"})
    configs: list = []

    def read_configs():
        for path in private_tmp.rglob("*"):
            if path.is_file() and not path.parent.name.startswith("ssh-"):
                configs.append(path.read_bytes())

    git_spy.before("push", read_configs)
    report = push(target, account=account, logs=logs)
    assert report.kind == "merged"
    assert configs, "the temporary clone was inspected before the push"
    for blob in configs:
        assert not any(form.encode() in blob for form in forms)
    for argv, env in git_spy.calls:
        assert not any(form in word for form in forms for word in argv)
        leaked = [k for k, v in env.items() if any(form in v for form in forms)]
        assert leaked == (["GIT_CONFIG_VALUE_0"] if auth != "ssh_key" and "GIT_CONFIG_VALUE_0" in env else [])
    assert list(private_tmp.iterdir()) == []          # no session folder, no key file left
    texts = [*logs, cloned_report.note(), report.note()]
    assert not any(form in text for form in forms for text in texts)
    for data in snapshot(ws).values():
        assert not any(form.encode() in data for form in forms)
    public = {k: v for k, v in snapshot_.items() if k not in {"secrets", "secret_refs", "secret_fields"}}
    assert not any(form in json.dumps(public) for form in forms)
    # An error names the account's fix, never its secret.
    hub.remove(remote)
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(remote, "api"), account=account)
    assert not any(form in str(failure.value) for form in forms)


@needs_git
def test_git_children_never_see_tool_tokens_or_ssh_agent(ws, fake_remote, git_spy, monkeypatch):
    for name in git_run._DROPPED | {"GIT_TRACE", "GIT_SSH_COMMAND"}:
        monkeypatch.setenv(name, "ambient")
    target = cloned(fake_remote, ws)
    (ws / "api/README.md").write_text("edited\n")
    push(target)
    assert git_spy.calls
    for _argv, env in git_spy.calls:
        assert not (git_run._DROPPED | {"GIT_TRACE", "GIT_SSH_COMMAND"}) & env.keys()


@needs_git
def test_cell_symlinks_and_special_files_are_refused_and_git_names_never_synced(ws, fake_remote, monkeypatch):
    target = cloned(fake_remote, ws)
    (ws / "api/.git").write_text("gitdir: /elsewhere\n")
    (ws / "api/src/.GIT").mkdir()
    (ws / "api/src/.GIT/x").write_text("x\n")
    (ws / "api/README.md").write_text("edited\n")
    logs: list = []
    report = push(target, logs=logs)
    assert report.kind == "pushed" and report.local_edits == 1
    assert "2 files under node_modules/.venv/__pycache__ in api/ are never synced" in logs
    (ws / "api/alias").symlink_to(ws / "api/README.md")
    before = snapshot(ws)
    with pytest.raises(repositories.RepositoryError) as failure:
        push(target)
    assert str(failure.value) == repositories.R8.format(f="api", p="alias")
    (ws / "api/alias").unlink()
    monkeypatch.setattr(repositories, "FILE_CAP", 10)
    (ws / "api/docs/c.txt").write_text("more than ten bytes\n")
    before["api/docs/c.txt"] = b"more than ten bytes\n"
    with pytest.raises(repositories.RepositoryError) as failure:
        push(target)
    assert str(failure.value).endswith("is not a regular file of at most 20 MB, so it cannot be synced. Nothing changed")
    assert snapshot(ws) == {k: v for k, v in before.items() if k != "api/alias"}


# ── the account's reach ───────────────────────────────────────────────


@pytest.mark.parametrize("case", ["A1", "A2", "A3", "A4", "A5"])
@pytest.mark.parametrize("operation", ["sync", "push"])
def test_a1_to_a5_spawn_no_git_process(cell, ws, git_spy, case, operation):
    account, remote, expected = {
        "A1": (ACCOUNT, GL, repositories.A1.format(base="github.com", host="example.com")),
        "A2": (None, SSH, repositories.A2.format(r="box.example.org:2222/srv/app")),
        "A3": (BASIC, "git@git.example.org:team/app.git",
               repositories.A3.format(method="a username and token", scheme="SSH", other="https")),
        "A4": (BOX, "ssh://git@other.example/app.git", repositories.A4.format(hostport="other.example")),
        "A5": (BOX, SSH, repositories.A5),
    }[case]
    if case == "A5":
        cell.permissions = {"internet": "proxy"}
    if operation == "push" and account is None:
        account = NONE
        expected = repositories.A3.format(method="no credentials", scheme="SSH", other="https")
    write_marker(ws, "app", repositories.Marker(remote, "main", "a" * 40).data())
    before = snapshot(ws)
    with pytest.raises(repositories.RepositoryError) as failure:
        if operation == "sync":
            sync(T(remote, "app"), account=account)
        else:
            push(T(remote, "app"), account=account)
    assert str(failure.value) == expected
    assert git_spy.calls == []
    assert snapshot(ws) == before


def test_a6_public_account_push_spawns_no_git_process(cell, ws, git_spy):
    write_marker(ws, "api", repositories.Marker(GH, "main", "a" * 40).data())
    (ws / "api/README.md").write_text("edited\n")
    before = snapshot(ws)
    with pytest.raises(repositories.RepositoryError) as failure:
        push(T(GH, "api"), account=NONE)
    assert str(failure.value) == repositories.A6.format(r="github.com/octo/api")
    assert git_spy.calls == []
    assert snapshot(ws) == before


@needs_git
def test_rejected_credential_is_never_retried_anonymously(ws, fake_remote, git_spy):
    fake_remote.create(GH, BASE)
    git_spy.fail("ls-remote", code=128, stderr=b"remote: Invalid username or token.\nfatal: Authentication failed")
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(GH, "api"), account=ACCOUNT)
    assert str(failure.value) == repositories.N4.format(host="github.com", cred="token")
    assert spawned(git_spy) == ["ls-remote"]
    assert "GIT_CONFIG_VALUE_0" in git_spy.calls[-1][1]


@needs_git
def test_sync_merge_commits_carry_the_account_identity_when_bound(ws, fake_remote, git_spy):
    target = cloned(fake_remote, ws, account=ACCOUNT)
    (ws / "api/src/a.py").write_text("a = 2\n")
    fake_remote.commit(GH, {"docs/c.txt": "moved\n"})
    sync(target, account=ACCOUNT)
    merges = [env for argv, env in git_spy.calls if subcommand_of(argv) in ("merge", "commit")]
    assert merges and all((env["GIT_AUTHOR_NAME"], env["GIT_COMMITTER_EMAIL"]) == ("Ada Lovelace", "ada@example.org")
                          for env in merges)
    git_spy.calls.clear()
    (ws / "api/src/b.py").write_text("b = 2\n")
    fake_remote.commit(GH, {"docs/d.txt": "d\n"})
    sync(target, account=None)
    merges = [env for argv, env in git_spy.calls if subcommand_of(argv) in ("merge", "commit")]
    assert merges and all(env["GIT_AUTHOR_NAME"] == "Collimate" for env in merges)


@needs_git
def test_push_commits_as_the_account_identity(ws, fake_remote):
    target = cloned(fake_remote, ws)
    (ws / "api/README.md").write_text("edited\n")
    gl_identity = make_account(metadata={"author_name": "Grace Hopper", "author_email": "grace@example.com"})
    report = push(target, account=gl_identity)
    who = fake_remote.git(GH, "log", "-1", "--format=%an <%ae>|%cn <%ce>", report.pushed).decode().strip()
    assert who == "Grace Hopper <grace@example.com>|Grace Hopper <grace@example.com>"


@needs_git
def test_gitlab_nested_group_clone_update_push(ws, fake_remote):
    target = cloned(fake_remote, ws, remote=GL, account=GLA)
    assert target == T(GL, "app")
    assert marker(ws, "app")["remote"] == GL
    head = fake_remote.commit(GL, {"docs/new.txt": "n\n"})
    report = sync(target, account=GLA)
    assert report.kind == "updated" and marker(ws, "app")["commit"] == head
    (ws / "app/README.md").write_text("edited\n")
    pushed = push(target, account=GLA, push_branch="fix")
    assert pushed.kind == "new_branch" and fake_remote.head(GL, "fix") == pushed.pushed
    assert pushed.compare_url == "https://example.com/gitlab/group/sub/app/-/compare/main...fix"


@needs_git
def test_bound_account_goes_through_auth_for_under_fake_remote(ws, fake_remote, git_spy, monkeypatch):
    seen: list = []
    original = git_account.GitAccount.auth_for

    def spy(self, remote):
        seen.append(remote)
        return original(self, remote)

    monkeypatch.setattr(git_account.GitAccount, "auth_for", spy)
    target = cloned(fake_remote, ws, account=ACCOUNT)
    (ws / "api/README.md").write_text("edited\n")
    assert push(target, account=ACCOUNT).kind == "pushed"
    assert seen and all(r == git_run.Remote(GH, ("https",)) for r in seen)
    git_spy.calls.clear()
    write_marker(ws, "app", repositories.Marker(GL, "main", "a" * 40).data())
    with pytest.raises(repositories.RepositoryError) as failure:
        push(T(GL, "app"), account=ACCOUNT)
    assert str(failure.value) == repositories.A1.format(base="github.com", host="example.com")
    assert git_spy.calls == []


@needs_git
def test_protected_branch_markers_include_gitlab_pre_receive(ws, fake_remote, git_spy):
    target = cloned(fake_remote, ws, remote=GL, account=GLA)
    r = repositories.display_of(GL)
    for summary, expected in (
            ("! [remote rejected] (pre-receive hook declined)",
             repositories.P5.format(host="example.com", r=r, dst="main")),
            ("! [remote rejected] (protected branch hook declined)",
             repositories.P5.format(host="example.com", r=r, dst="main")),
            ("! [remote rejected] (You are not allowed to push code to this project.)",
             repositories.P6.format(r=r, cred="token", hint6=" (write_repository, and the Developer role or above)")),
            ("! [remote rejected] (hook declined)", repositories.P5b.format(host="example.com", r=r, dst="main"))):
        (ws / "app/README.md").write_text(f"edited {summary}\n")
        git_spy.fail("push", code=1, stdout=_status(summary))
        with pytest.raises(repositories.RepositoryError) as failure:
            push(target, account=GLA)
        assert str(failure.value) == expected


# ── SSH through the fake ssh ──────────────────────────────────────────


@needs_git
@pytest.mark.parametrize("remote, account_name", [
    (SSH, "BOX"), ("git@box.example.org:team/app.git", "BOX"), (GH, "GH_SSH")])
def test_ssh_clone_update_push_end_to_end(ws, fake_ssh, remote, account_name):
    account = {"BOX": BOX, "GH_SSH": GH_SSH}[account_name]
    hub = fake_ssh.hub
    target = cloned(hub, ws, remote=remote, account=account)
    folder = target.folder
    assert marker(ws, folder)["remote"] == remote
    head = hub.commit(remote, {"docs/new.txt": "n\n"})
    assert sync(target, account=account).kind == "updated" and marker(ws, folder)["commit"] == head
    (ws / folder / "README.md").write_text("edited over ssh\n")
    report = push(target, account=account)
    assert report.kind == "pushed" and hub.head(remote) == report.pushed
    calls = fake_ssh.calls()
    assert {c["cmd"] for c in calls} == {"git-upload-pack", "git-receive-pack"}
    expected_path = {SSH: "/srv/app.git", GH: "/octo/api.git"}.get(remote, "team/app.git")
    assert {c["path"] for c in calls} == {expected_path}
    assert all(c["key_mode"] == 0o600 and c["dir_mode"] == 0o700 and c["user"] == "git" for c in calls)
    assert all("SSH_AUTH_SOCK" not in c["env_has"] and "GIT_CONFIG_COUNT" not in c["env_has"] for c in calls)


@needs_git
def test_scp_record_reaches_ssh_with_the_path_git_clone_sends(tmp_path, ws, fake_ssh, private_tmp):
    hub = fake_ssh.hub
    scp = "git@box.example.org:team/r.git"
    hub.create(scp, BASE)
    known = tmp_path / "known_hosts"
    known.write_text(f"box.example.org {_ED}\n")
    env = {**os.environ, "GIT_SSH_COMMAND": f"{fake_ssh.command()} -o UserKnownHostsFile={known}",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "clone", "--quiet", scp, str(tmp_path / "plain")], env=env, check=True,
                   capture_output=True, timeout=60)
    plain = fake_ssh.calls()[-1]["path"]
    sync(T(scp, "r"), account=BOX)
    engine = fake_ssh.calls()[-1]["path"]
    assert plain == engine == "team/r.git"
    sync(T("ssh://git@box.example.org/team/r.git", "r2"), account=BOX)
    assert fake_ssh.calls()[-1]["path"] == "/team/r.git"


@needs_git
def test_unknown_ssh_host_refuses_n8(ws, fake_ssh, monkeypatch):
    fake_ssh.hub.create(SSH, BASE)
    monkeypatch.setenv("FAKE_SSH_REFUSE", "host_key")
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(SSH, "app"), account=BOX)
    assert str(failure.value) == repositories.N8.format(hostport="[box.example.org]:2222")
    monkeypatch.setenv("FAKE_SSH_REFUSE", "publickey")
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(SSH, "app"), account=BOX)
    fp = git_hosts.inspect_private_key(ssh_key()).fingerprint
    assert str(failure.value) == repositories.N4s.format(host="box.example.org:2222", fp=fp)
    assert not (ws / "app").exists()


@needs_git
def test_missing_ssh_refuses_e5(ws, fake_ssh, monkeypatch, tmp_path):
    fake_ssh.hub.create(SSH, BASE)
    git_dir = Path(shutil.which("git")).parent
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(git_dir / "git")
    monkeypatch.setenv("PATH", str(only_git))
    with pytest.raises(repositories.RepositoryError) as failure:
        sync(T(SSH, "app"), account=BOX)
    assert str(failure.value) == repositories.E5
