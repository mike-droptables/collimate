# SPDX-License-Identifier: MIT
"""The built-in ``git-checkout`` handler (builtins/definitions/git-checkout):
its verifier agrees with the engine's ``read_marker`` rule for rule, accepts
every record the engine writes, checks only cell-root records, and its skill's
git recipe runs against a fake remote without touching the cell."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from collimate._host.renderer_bundle import compile_verifier
from collimate._host.verification import run_verifier
from collimate.handlers import derived_hash, handler_for
from _env_shared import repositories
from tests._git_fixtures import GH, GIT_OK, GL
from tests._workspace import BUILTINS

needs_git = pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = BUILTINS / "definitions" / "git-checkout"
SKILL = PACKAGE / "derived" / "skills" / "git-operations" / "SKILL.md"
SHA = "3f786850e387550fdab836ed7e6dc881de23001b"
RECORD = "api.git-checkout.json"
PREFIX = "Not a valid Git checkout record ("
TEXT_LIMIT = 1024 * 1024


@pytest.fixture(scope="module")
def verifier() -> str:
    return compile_verifier((PACKAGE / "derived" / "verify.ts").read_bytes(),
                            prelude=(ROOT / "shared" / "verify" / "prelude.js").read_bytes()).code


def _content(body: bytes):
    """A file as the verifier sees it: text, or opaque bytes metadata for
    non-UTF-8, NUL-holding or oversized files (api/verification.py)."""
    if len(body) <= TEXT_LIMIT:
        try:
            text = body.decode("utf-8")
            if "\x00" not in text:
                return text
        except UnicodeError:
            pass
    return {"size": len(body), "hash": hashlib.sha256(body).hexdigest()}


def _input(path: str, files: dict[str, bytes]) -> dict:
    """``api/verification.verifier_input`` with ``root_is_package`` (a handler
    example's folder is its own root)."""
    prefix = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
    own = _content(files[path])
    return {"path": path, "text" if isinstance(own, str) else "bytes": own,
            "package": {name[len(prefix):]: _content(body) for name, body in files.items()
                        if name.startswith(prefix)},
            "cell_paths": sorted(files)}


def verify(code: str, path: str, body: "bytes | str") -> dict:
    data = body.encode("utf-8") if isinstance(body, str) else body
    return run_verifier(code, _input(path, {path: data}), deadline=time.monotonic() + 5)


def _record(**fields) -> dict:
    """The shipped record (the engine's bytes, pinned below) with ``fields``."""
    return {**json.loads((PACKAGE / "examples" / "cloned" / RECORD).read_text()), **fields}


def _dumps(data) -> str:
    """Non-ASCII stays raw, so sizes count real UTF-8 bytes."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def engine_reason(workspace: Path, body: bytes) -> "str | None":
    """``read_marker``'s F3 reason for ``body`` as api's record, or None
    when it accepts it."""
    (workspace / "api").mkdir(exist_ok=True)
    (workspace / repositories.marker_path("api")).write_bytes(body)
    head, tail = repositories.F3.format(f="api", reason="\0").split("\0")
    try:
        repositories.read_marker(workspace, "api")
    except repositories.RepositoryError as exc:
        match = re.fullmatch(re.escape(head) + "(.*)" + re.escape(tail), str(exc), re.DOTALL)
        assert match, str(exc)
        return match[1]
    return None


# ── the shipped package ──────────────────────────────────────────────────


def test_shipped_examples_meet_their_expectations_and_hash_matches(verifier):
    manifest = yaml.safe_load((PACKAGE / "git-checkout.handler").read_text())
    derived = PACKAGE / "derived"
    assert manifest["derived"] == derived_hash(
        {p.relative_to(derived).as_posix(): p.read_bytes() for p in derived.rglob("*") if p.is_file()})
    assert manifest["examples"]
    for example in manifest["examples"]:
        folder = PACKAGE / "examples" / example["folder"]
        files = {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
        covered = [path for path in sorted(files) if handler_for([manifest], path) is not None]
        assert covered, example["folder"]
        for path in covered:
            verdict = run_verifier(verifier, _input(path, files), deadline=time.monotonic() + 5)
            assert verdict["valid"] is (example["expect"] == "valid"), (example["folder"], path, verdict)


def test_cloned_example_is_the_engine_bytes():
    assert (PACKAGE / "examples" / "cloned" / RECORD).read_text() == repositories.Marker(
        "https://github.com/octo/api.git", "main", SHA).text()


REMOTE_512 = "https://h.io" + ("/" + "a" * 99) * 5              # exactly REMOTE_CAP code points
SEGMENTS_32 = "https://h.io" + "/s" * 32


@pytest.mark.parametrize("marker", [
    repositories.Marker(GH, "main", SHA),
    repositories.Marker(GH, "main", "0123456789abcdef" * 4),
    repositories.Marker(GH, '"' * 255, SHA),
    repositories.Marker(GH, "funktion/größe", SHA),
    repositories.Marker("https://gitlab.com/group/sub/deeper/api.git", "main", SHA),
    repositories.Marker("ssh://git@box.example.org:2222/srv/api.git", "main", SHA),
    repositories.Marker("ssh://git@box.example.org/~/api.git", "main", SHA),
    repositories.Marker("git@box.example.org:team/api.git", "main", SHA),
    repositories.Marker(REMOTE_512, "main", SHA),
    repositories.Marker(SEGMENTS_32, "main", SHA),
], ids=["sha1", "sha256", "quote-branch", "non-ascii-branch", "gitlab-nested", "ssh-port", "ssh-tilde", "scp",
        "remote-512", "segments-32"])
def test_accepts_every_record_the_engine_writes(verifier, marker, tmp_path):
    assert len(REMOTE_512) == repositories.REMOTE_CAP
    text = marker.text()
    assert len(text.encode("utf-8")) <= repositories.MARKER_CAP
    assert verify(verifier, RECORD, text) == {"valid": True}
    assert engine_reason(tmp_path, text.encode("utf-8")) is None


# ── parity with the engine ───────────────────────────────────────────────


def _body(**fields) -> str:
    return _dumps(_record(**fields))


def _without(key: str) -> str:
    data = _record()
    del data[key]
    return _dumps(data)


def _raw_branch(escaped: str) -> str:
    """A record whose branch is the JSON string literal ``escaped``."""
    return ('{"about":"x","remote":"https://h.io/o/r","branch":"' + escaped + '","commit":"' + "a" * 40 + '"}')


# Remotes the engine's check_remote and verify.ts's remoteOk both accept, then both refuse.
REMOTES_OK = [
    "https://github.com/octo/api.git", "https://github.com/octo/api", "https://github.com/octo/.github.git",
    "https://gitlab.com/a/b/c/d.git", "https://example.com/gitlab/team/app.git", "https://git.example.com:8443/x/y",
    "https://dev.azure.com/org/My%20Project/_git/app",
    "https://gitserver/r.git",                                       # single-label host
    "ssh://git@host/srv/r.git", "ssh://git@host:2222/~/r.git", "ssh://deploy@host/~deploy/r",
    "git@host:team/r.git", "git@host:~/r.git", "git@host:/srv/r.git", "deploy@host:~deploy/r",
    "APKAEIBAERJR2EXAMPLE@git-codecommit.us-east-1.amazonaws.com:v1/repos/app",      # uppercase user
    "ssh://APKAEIBAERJR2EXAMPLE@git-codecommit.us-east-1.amazonaws.com/v1/repos/app",
]
REMOTES_BAD = [
    "octo/api", "http://github.com/o/a", "git://h/r", "file:///r",
    "HTTPS://github.com/o/a", "https://GitHub.com/o/a", "https://github.com:443/o/a", "ssh://git@h:22/r",
    "https://u@github.com/o/a", "https://u:p@github.com/o/a", "ssh://git:p@h/r", "ssh://h/r",
    "ssh://-oProxyCommand=x/r", "ssh://git@-h/r",
    "https://github.com/o/a?x=1", "https://github.com/o/a#f",
    "https://github.com/o/../a", "https://github.com/o/./a", "https://github.com/o//a", "https://github.com/o/a/",
    "https://github.com/o/a b", "https://github.com/o/a'b", "https://github.com/o/$x", "https://github.com/-o/a",
    "https://[::1]/r", "https://github.com/~/r", "ssh://git@h/r/~/x", "ssh://git@h/~",
    "host:team/r.git",                                               # scp without a user; the engine writes git@
    "git@host:", "git@host:~", "git@host:-x", "git@Host:r", "git:p@host:r", "git@host:r/~/x", "git@host:a/../b",
    "-git@host:r",
    "https://h/a%2Fb", "https://h/a%2fb", "https://h/a%zz", "https://h/%2E%2E/x",
    "https://h:0/x", "https://h:65536/x", "https://h:080/x",
]


PARITY = [
    # rule 2: size, counted in UTF-8 bytes
    ("five-thousand", "x" * 5000, "larger than 4 KB"),
    ("multibyte-over-cap", _body(about="é" * 2100), "larger than 4 KB"),
    # rule 3: UTF-8 JSON
    ("not-json", "not json", "not UTF-8 JSON"),
    ("empty", "", "not UTF-8 JSON"),
    ("nan", '{"about":"x","remote":"https://h.io/o/r","branch":"main","commit":NaN}', "not UTF-8 JSON"),
    ("infinity", '{"about":"x","remote":"https://h.io/o/r","branch":"main","commit":Infinity}', "not UTF-8 JSON"),
    ("trailing-comma", '{"about":"x","remote":"https://h.io/o/r","branch":"main","commit":"' + "a" * 40 + '",}',
     "not UTF-8 JSON"),
    ("not-utf8", b"\xff\xfe{}", "not UTF-8 JSON"),
    # rule 4: exactly the four keys
    ("array", "[]", "not an object with exactly about, remote, branch and commit"),
    ("null", "null", "not an object with exactly about, remote, branch and commit"),
    ("string", '"octo/api"', "not an object with exactly about, remote, branch and commit"),
    ("empty-object", "{}", "not an object with exactly about, remote, branch and commit"),
    ("missing-about", _without("about"), "not an object with exactly about, remote, branch and commit"),
    ("missing-commit", _without("commit"), "not an object with exactly about, remote, branch and commit"),
    ("extra-url", _body(url="https://github.com/octo/api"),
     "not an object with exactly about, remote, branch and commit"),
    # rule 5: about
    ("about-empty", _body(about=""), "bad about"),
    ("about-501", _body(about="x" * 501), "bad about"),
    ("about-number", _body(about=7), "bad about"),
    ("about-null", _body(about=None), "bad about"),
    ("about-first", _body(about=7, remote="nope", commit="main"), "bad about"),
    ("about-500", _body(about="ü" * 500), None),
    # rule 6: remote (repositories.check_remote); REMOTES_OK / REMOTES_BAD below
    ("remote-513", _body(remote=REMOTE_512 + "b"), "bad remote"),
    ("remote-33-segments", _body(remote=SEGMENTS_32 + "/s"), "bad remote"),
    ("remote-newline", _body(remote=GH + "\n"), "bad remote"),
    ("remote-number", _body(remote=7), "bad remote"),
    ("remote-null", _body(remote=None), "bad remote"),
    ("remote-list", _body(remote=["https://h/r"]), "bad remote"),
    ("remote-before-branch", _body(remote="x", branch=""), "bad remote"),
    # rule 7: branch
    ("branch-dotdot", _body(branch="a..b"), "bad branch"),
    ("branch-empty", _body(branch=""), "bad branch"),
    ("branch-dash", _body(branch="-x"), "bad branch"),
    ("branch-slash-start", _body(branch="/x"), "bad branch"),
    ("branch-slash-end", _body(branch="x/"), "bad branch"),
    ("branch-dot-end", _body(branch="x."), "bad branch"),
    ("branch-at", _body(branch="@"), "bad branch"),
    ("branch-head", _body(branch="HEAD"), "bad branch"),
    ("branch-double-slash", _body(branch="a//b"), "bad branch"),
    ("branch-at-brace", _body(branch="a@{b"), "bad branch"),
    ("branch-dot-component", _body(branch="a/.b"), "bad branch"),
    ("branch-lock", _body(branch="a/b.lock"), "bad branch"),
    ("branch-space", _body(branch="a b"), "bad branch"),
    ("branch-colon", _body(branch="a:b"), "bad branch"),
    ("branch-backslash", _body(branch="a\\b"), "bad branch"),
    ("branch-tab", _body(branch="a\tb"), "bad branch"),
    ("branch-del", _body(branch="a\x7fb"), "bad branch"),
    ("branch-256-bytes", _body(branch="b" * 256), "bad branch"),
    ("branch-number", _body(branch=7), "bad branch"),
    ("branch-lone-surrogate", _raw_branch("\\ud800"), "bad branch"),
    ("branch-255-bytes", _body(branch="ß" * 127 + "b"), None),
    # rule 8: commit
    ("commit-short", _body(commit="abc"), "bad commit"),
    ("commit-option", _body(commit="--upload-pack=x"), "bad commit"),
    ("commit-branch", _body(commit="main"), "bad commit"),
    ("commit-upper", _body(commit=SHA.upper()), "bad commit"),
    ("commit-41", _body(commit=SHA + "a"), "bad commit"),
    ("commit-number", _body(commit=7), "bad commit"),
    # duplicate keys keep the last value on both sides
    ("duplicate-about", '{"about":"first","about":"x","remote":"https://h.io/o/r","branch":"main","commit":"'
     + "a" * 40 + '"}', None),
    ("duplicate-commit-last-bad", '{"about":"x","remote":"https://h.io/o/r","branch":"main","commit":"'
     + "a" * 40 + '","commit":"main"}', "bad commit"),
    ("valid", _body(), None),
    *[(f"remote-ok:{remote}", _body(remote=remote), None) for remote in REMOTES_OK],
    *[(f"remote-bad:{remote}", _body(remote=remote), "bad remote") for remote in REMOTES_BAD],
]


@pytest.mark.parametrize("body,reason", [case[1:] for case in PARITY], ids=[case[0] for case in PARITY])
def test_verifier_and_engine_agree(verifier, tmp_path, body, reason):
    data = body.encode("utf-8") if isinstance(body, str) else body
    assert engine_reason(tmp_path, data) == reason
    expected = {"valid": True} if reason is None else {
        "valid": False, "error": f"{PREFIX}{reason})"}
    assert verify(verifier, RECORD, data) == expected


def test_root_name_case_variant_is_still_checked(verifier):
    assert verify(verifier, "API.Git-Checkout.json", "{}")["valid"] is False
    assert verify(verifier, "API.Git-Checkout.json", _dumps(_record())) == {"valid": True}


def test_nested_file_is_repository_content(verifier):
    for path in ("a/b.git-checkout.json", "api/tests/x.git-checkout.json"):
        assert verify(verifier, path, "junk") == {"valid": True}
        assert verify(verifier, path, b"\xff\xfe") == {"valid": True}


def test_bytes_input_is_refused_with_the_engine_reason(verifier):
    def run(size):
        return run_verifier(verifier, {"path": RECORD, "bytes": {"size": size, "hash": "0" * 64},
                                       "package": {}, "cell_paths": [RECORD]}, deadline=time.monotonic() + 5)
    assert run(10) == {"valid": False, "error": f"{PREFIX}not UTF-8 JSON)"}
    assert run(5000) == {"valid": False, "error": f"{PREFIX}larger than 4 KB)"}


# ── the skill ────────────────────────────────────────────────────────────


def _cell_bytes(cell: Path) -> dict[str, bytes]:
    return {p.relative_to(cell).as_posix(): p.read_bytes() for p in cell.rglob("*") if p.is_file()}


def _recipe(remote: str, commit: str, extra: str, *, folder: str = "api") -> str:
    """The skill's setup for the record ``remote`` at ``commit``, then ``extra``."""
    script = re.search(r"```sh\n(.*?)```", SKILL.read_text(), re.DOTALL)[1]
    values = {"FOLDER": folder, "REMOTE": remote, "BRANCH": "main", "COMMIT": commit}
    for name, value in values.items():
        script, count = re.subn(rf"^{name}='[^']*'", lambda _m: f"{name}='{value}'", script, flags=re.MULTILINE)
        assert count == 1, name
    assert script.count('-- "$REMOTE"') == 1
    return script + extra


def _run_recipe(script: str, cell: Path, tmp_path: Path, **env) -> subprocess.CompletedProcess:
    temp, home = tmp_path / "tmp", tmp_path / "home"
    temp.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    return subprocess.run(["sh", "-c", script], cwd=cell, capture_output=True, text=True, timeout=120,
                          env={"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(temp), **env})


def _edited_cell(tmp_path: Path, remote: str, base: str) -> Path:
    cell = tmp_path / "cell"
    (cell / "api").mkdir(parents=True)
    (cell / "api" / "a.txt").write_text("two\n")
    (cell / "api" / "new.txt").write_text("new\n")
    (cell / RECORD).write_text(repositories.Marker(remote, "main", base).text())
    return cell


@needs_git
def test_skill_recipe_runs_against_a_fake_repository(fake_remote, tmp_path):
    base = fake_remote.create(GH, {"a.txt": "one\n", "b.txt": "gone\n"})
    fake_remote.commit(GH, {"c.txt": "upstream\n"}, message="upstream change")
    cell = _edited_cell(tmp_path, GH, base)
    before = _cell_bytes(cell)

    script = _recipe(fake_remote.url(GH), base,
                     'G diff --cached --name-status "$COMMIT"; R log --oneline "$COMMIT..$BRANCH"; '
                     'G blame --contents "$CELL/$FOLDER/a.txt" "$COMMIT" -- a.txt\n')
    result = _run_recipe(script, cell, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert {"M\ta.txt", "A\tnew.txt", "D\tb.txt"} <= set(lines), result.stdout
    assert any(line.endswith(" upstream change") for line in lines), result.stdout
    assert "External file (--contents)" in result.stdout
    assert not [p for p in cell.rglob(".git")]
    assert _cell_bytes(cell) == before
    assert list((tmp_path / "tmp").iterdir()) == []


@needs_git
def test_skill_recipe_runs_for_a_gitlab_nested_group(fake_remote, tmp_path):
    base = fake_remote.create(GL, {"a.txt": "one\n", "b.txt": "gone\n"})
    cell = _edited_cell(tmp_path, GL, base)
    before = _cell_bytes(cell)

    result = _run_recipe(_recipe(fake_remote.url(GL), base, 'G diff --cached --name-status "$COMMIT"\n'),
                         cell, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {"M\ta.txt", "A\tnew.txt", "D\tb.txt"} <= set(result.stdout.splitlines()), result.stdout
    assert _cell_bytes(cell) == before
    assert list((tmp_path / "tmp").iterdir()) == []


@needs_git
def test_skill_recipe_keeps_harness_supplied_git_config(fake_remote, tmp_path):
    """The Agent's harness environment carries the Git account's scoped
    header as GIT_CONFIG_COUNT/KEY_0/VALUE_0; the recipe leaves it in place
    and still runs (the file remote ignores an http header)."""
    base = fake_remote.create(GH, {"a.txt": "one\n"})
    cell = _edited_cell(tmp_path, GH, base)
    script = _recipe(fake_remote.url(GH), base,
                     'G diff --cached --name-status "$COMMIT"; git config --get-regexp "^http\\." \n')
    result = _run_recipe(script, cell, tmp_path, GIT_CONFIG_COUNT="1",
                         GIT_CONFIG_KEY_0="http.https://github.com/.extraHeader",
                         GIT_CONFIG_VALUE_0="Authorization: Basic eDp5")
    assert result.returncode == 0, result.stdout + result.stderr
    assert {"M\ta.txt", "A\tnew.txt"} <= set(result.stdout.splitlines()), result.stdout
    assert "http.https://github.com/.extraheader Authorization: Basic eDp5" in result.stdout


@pytest.mark.parametrize("remote", ["ssh://git@h/r.git", "git@h:team/r.git"])
def test_skill_recipe_stops_at_an_ssh_remote_without_the_ssh_command(remote, tmp_path):
    cell = tmp_path / "cell"
    (cell / "api").mkdir(parents=True)
    (cell / RECORD).write_text(repositories.Marker(remote, "main", SHA).text())
    before = _cell_bytes(cell)
    result = _run_recipe(_recipe(remote, SHA, "echo unreachable\n"), cell, tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stderr == ("api/ is reached over SSH and no SSH key reached this harness (the Git account is "
                             "not an SSH account, or this harness runs in a container): ask the user to run "
                             "Git Clone / Update or Git Push\n")
    assert result.stdout == ""
    assert list((tmp_path / "tmp").iterdir()) == []
    assert _cell_bytes(cell) == before


@needs_git
@pytest.mark.parametrize("remote", ["ssh://git@box.example.org:2222/srv/api.git", "git@box.example.org:team/api.git"])
def test_skill_recipe_runs_over_ssh_with_the_harness_ssh_command(fake_ssh, ssh_keys, tmp_path, remote):
    """With an SSH account on this machine the harness carries
    GIT_SSH_COMMAND (git_run.ssh_command over the session's key and
    known_hosts); the recipe clones and fetches over it, leaves the cell
    untouched and never names the key on argv."""
    import shlex
    from collimate import git_hosts
    from _env_shared import git_run
    base = fake_ssh.hub.create(remote, {"a.txt": "one\n", "b.txt": "gone\n"})
    cell = _edited_cell(tmp_path, remote, base)
    before = _cell_bytes(cell)
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    key = git_hosts.normalise_private_key(ssh_keys["ed25519"])
    git_run.write_private(keys / "key", key)
    blob = ssh_keys.blob()
    git_run.write_private(keys / "known_hosts", f"[box.example.org]:2222,box.example.org ssh-ed25519 {blob}\n")
    command = git_run.ssh_command(fake_ssh.command(), keys / "key", keys / "known_hosts")

    result = _run_recipe(_recipe(remote, base, 'G diff --cached --name-status "$COMMIT"\n'), cell, tmp_path,
                         GIT_SSH_COMMAND=command, GIT_SSH_VARIANT="ssh",
                         FAKE_SSH_ROOT=str(fake_ssh.hub.root), FAKE_SSH_LOG=str(fake_ssh.log))
    assert result.returncode == 0, result.stdout + result.stderr
    assert {"M\ta.txt", "A\tnew.txt", "D\tb.txt"} <= set(result.stdout.splitlines()), result.stdout
    calls = fake_ssh.calls()
    assert calls and {call["host"] for call in calls} == {"box.example.org"}
    assert all(call["key_bytes_sha256"] == hashlib.sha256(key.encode()).hexdigest() for call in calls)
    assert all(shlex.join(call["argv"]).count(str(keys / "key")) == 1 for call in calls)
    assert not any(line in " ".join(call["argv"]) for call in calls for line in key.splitlines()[1:-1])
    assert _cell_bytes(cell) == before
    assert not [p for p in cell.rglob(".git")]
    assert list((tmp_path / "tmp").iterdir()) == []


FORGE_SSH = {"COLLIMATE_GIT_HTTPS_BASE": "https://gitlab.example.com:8443/git/",
             "COLLIMATE_GIT_SSH_BASE": "ssh://git@gitlab.example.com/"}


def _resolved_remote(remote: str, tmp_path: Path, **env) -> str:
    """The address the setup hands git for the record ``remote``: the
    recipe up to its temp folder, then ``REMOTE`` printed."""
    script = _recipe(remote, SHA, "")
    script = script[:script.index('WORK="$(mktemp')] + 'printf %s "$REMOTE"\n'
    cell = tmp_path / "cell"
    cell.mkdir(exist_ok=True)
    result = _run_recipe(script, cell, tmp_path, GIT_SSH_COMMAND="ssh", **env)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("remote, env, expected", [
    # A forge SSH account: the web base's port and path are dropped, one .git (git_run.resolve_remote).
    ("https://gitlab.example.com:8443/git/team/api.git", FORGE_SSH, "ssh://git@gitlab.example.com/team/api.git"),
    ("https://gitlab.example.com:8443/git/team/sub/api", FORGE_SSH, "ssh://git@gitlab.example.com/team/sub/api.git"),
    ("https://github.com/octo/api.git",
     {"COLLIMATE_GIT_HTTPS_BASE": "https://github.com/", "COLLIMATE_GIT_SSH_BASE": "ssh://git@github.com/"},
     "ssh://git@github.com/octo/api.git"),
    # Anything not under the account's base stays as recorded (anonymous https).
    ("https://github.com/octo/public.git", FORGE_SSH, "https://github.com/octo/public.git"),
    ("https://gitlab.example.com/git/team/api.git", FORGE_SSH, "https://gitlab.example.com/git/team/api.git"),
    ("https://gitlab.example.com:8443/gitx/team/api.git", FORGE_SSH,
     "https://gitlab.example.com:8443/gitx/team/api.git"),
    # A Git server SSH account carries no bases: an https record is never rewritten.
    ("https://github.com/octo/public.git", {}, "https://github.com/octo/public.git"),
    ("ssh://git@box.example.org:2222/srv/api.git", FORGE_SSH, "ssh://git@box.example.org:2222/srv/api.git"),
])
def test_skill_recipe_rewrites_only_records_under_the_forge_base(tmp_path, remote, env, expected):
    from types import SimpleNamespace
    from _env_shared import git_run
    assert _resolved_remote(remote, tmp_path, **env) == expected
    if env:
        https_base, ssh_base = env["COLLIMATE_GIT_HTTPS_BASE"], env["COLLIMATE_GIT_SSH_BASE"]
        account = SimpleNamespace(forge=True, transport="ssh", web=https_base.rstrip("/"))
        assert ssh_base == "ssh://git@" + https_base.split("/")[2].split(":")[0] + "/"
        assert git_run.resolve_remote(remote, account).url == expected


@needs_git
def test_skill_recipe_reaches_an_https_record_over_ssh_with_a_forge_account(fake_ssh, ssh_keys, tmp_path):
    """A GitLab SSH account under a port and a path: the recipe sends the
    https record over the harness's ssh command to the web host on port 22,
    which the session's known_hosts names."""
    from collimate import git_hosts
    from _env_shared import git_run
    record = "https://gitlab.example.com:8443/git/team/api.git"
    base = fake_ssh.hub.create("ssh://git@gitlab.example.com/team/api.git", {"a.txt": "one\n", "b.txt": "gone\n"})
    cell = _edited_cell(tmp_path, record, base)
    before = _cell_bytes(cell)
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    git_run.write_private(keys / "key", git_hosts.normalise_private_key(ssh_keys["ed25519"]))
    blob = ssh_keys.blob()
    git_run.write_private(keys / "known_hosts", f"gitlab.example.com ssh-ed25519 {blob}\n")
    command = git_run.ssh_command(fake_ssh.command(), keys / "key", keys / "known_hosts")

    result = _run_recipe(_recipe(record, base, 'G diff --cached --name-status "$COMMIT"\n'), cell, tmp_path,
                         GIT_SSH_COMMAND=command, GIT_SSH_VARIANT="ssh", **FORGE_SSH,
                         FAKE_SSH_ROOT=str(fake_ssh.hub.root), FAKE_SSH_LOG=str(fake_ssh.log))
    assert result.returncode == 0, result.stdout + result.stderr
    assert {"M\ta.txt", "A\tnew.txt", "D\tb.txt"} <= set(result.stdout.splitlines()), result.stdout
    calls = fake_ssh.calls()
    assert calls and {(call["host"], call["port"], call["path"]) for call in calls} == {
        ("gitlab.example.com", None, "/team/api.git")}
    assert _cell_bytes(cell) == before
    assert list((tmp_path / "tmp").iterdir()) == []


@needs_git
def test_skill_recipe_lists_what_push_would_send_in_one_fetch(fake_remote, tmp_path):
    """A repository with ``* text=auto``, an executable the cell holds without
    its +x bit, and a harness whose global git config converts line endings
    and ignores files: the recipe ignores that config and the file mode as
    the runners do, and fetches the commit's files in one batch instead of
    one fetch per file inside ``G add``."""
    files = {".gitattributes": "* text=auto\n", "run.sh": ("exec", "#!/bin/sh\n"), "crlf.txt": b"one\r\n",
             **{f"f{i}.txt": f"{i}\n" for i in range(20)}}
    base = fake_remote.create(GH, files)
    cell = tmp_path / "cell"
    (cell / "api").mkdir(parents=True)
    for path, content in files.items():
        data = content[1] if isinstance(content, tuple) else content
        (cell / "api" / path).write_bytes(data.encode() if isinstance(data, str) else data)
    (cell / "api" / "f0.txt").write_text("edited\n")
    (cell / "api" / "new.txt").write_text("new\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / "ignored").write_text("new.txt\n")
    (home / ".gitconfig").write_text(f"[core]\n\tautocrlf = true\n\texcludesFile = {home / 'ignored'}\n")
    trace = tmp_path / "trace.json"

    result = _run_recipe(_recipe(fake_remote.url(GH), base, 'G diff --cached --name-status "$COMMIT"\n'), cell,
                         tmp_path, GIT_TRACE2_EVENT=str(trace))
    assert result.returncode == 0, result.stdout + result.stderr
    statuses = {line for line in result.stdout.splitlines() if re.match(r"[A-Z]\t", line)}
    assert statuses == {"A\tnew.txt", "M\tf0.txt"}, result.stdout
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    lazy = [e for e in events if e.get("event") == "child_start" and "fetch" in e.get("argv", [])]
    assert lazy == []


def test_skill_text_keeps_the_safety_rules():
    text = SKILL.read_text()
    for rule in ("GIT_CONFIG_COUNT=1", "--filter=blob:none", "trap 'rm -rf \"$WORK\"' EXIT",
                 'GIT_INDEX_FILE="$WORK/index"', 'case "$REMOTE" in ssh://*|*@*:*) [ -n "${GIT_SSH_COMMAND:-}" ] ||',
                 "GIT_CONFIG_GLOBAL=/dev/null",
                 "Never read, copy, print or move the key file that `GIT_SSH_COMMAND` names",
                 "Never add the key to `~/.ssh` or to an ssh agent",
                 "Never change the ssh options of `GIT_SSH_COMMAND`", "StrictHostKeyChecking=no",
                 "Never set, override, print or unset `GIT_SSH_COMMAND`, `GIT_SSH_VARIANT` or the two "
                 "`COLLIMATE_GIT_*` names", "never rewrite it to SSH yourself",
                 'if [ -n "${COLLIMATE_GIT_SSH_BASE:-}" ]; then case "$REMOTE" in "$COLLIMATE_GIT_HTTPS_BASE"*)',
                 "R config core.fileMode false", "R config core.autocrlf false",
                 "Never create `.git` anywhere in the cell",
                 "Never commit, tag or push from the temporary clone"):
        assert rule in text, rule
    assert "replaced by `ssh://git@`" not in text  # the agent never derives an SSH address by itself
