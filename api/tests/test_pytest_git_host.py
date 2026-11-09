"""Host git (``api/collimate_server/git_host.py``): the capability probe's four states, its
cache, and the hardened, discovery-free child every host git call is."""
from __future__ import annotations

import os
import stat
import sys
import time

import pytest

import collimate_server.git_host as git_host
from git_repos import GIT_OK


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    """Each test probes afresh, with its data dir (and ``.collimate/tmp``) in ``tmp_path``."""
    monkeypatch.chdir(tmp_path)
    git_host.reset_capability()
    yield
    git_host.reset_capability()


def _fake_git(tmp_path, monkeypatch, body: str) -> str:
    """A ``git`` script first and alone on PATH; its directory."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "git"
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    return str(bin_dir)


@GIT_OK
def test_this_machines_git_is_ok():
    cap = git_host.capability()
    assert cap.state == "ok" and cap.ok
    assert cap.version is not None and cap.version[:2] >= (2, 31)
    assert cap.path and os.path.isabs(cap.path)


def test_no_git_on_path_is_missing(tmp_path, monkeypatch):
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    cap = git_host.capability()
    assert (cap.state, cap.ok, cap.describe()) == ("missing", False, "missing")
    with pytest.raises(git_host.GitFailed) as failed:
        git_host.run(["--version"])
    assert failed.value.returncode is None
    assert "git on this machine is missing" in failed.value.stderr


@pytest.mark.parametrize("body", [
    "import sys\nsys.exit(1)\n",                                   # non-zero exit
    "print('not a version')\n",                                    # unparsable --version
    "import time\ntime.sleep(30)\n",                               # timeout
])
def test_a_git_that_does_not_run_is_broken(tmp_path, monkeypatch, body):
    _fake_git(tmp_path, monkeypatch, body)
    monkeypatch.setattr(git_host, "_PROBE_TIMEOUT", 1.0)
    cap = git_host.capability()
    assert (cap.state, cap.describe()) == ("broken", "does not run")


def test_a_git_that_cannot_be_executed_is_broken(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    (bin_dir / "git").write_bytes(b"\x7fELF not really")
    (bin_dir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert git_host.capability().state == "broken"


def test_a_git_below_the_floor_is_too_old(tmp_path, monkeypatch):
    _fake_git(tmp_path, monkeypatch, "print('git version 2.30.9')\n")
    cap = git_host.capability()
    assert (cap.state, cap.version, cap.ok) == ("too_old", (2, 30, 9), False)
    assert cap.describe() == "2.30.9, older than 2.31"


def test_the_probe_runs_once_and_is_cached_until_reset(tmp_path, monkeypatch):
    calls = tmp_path / "calls"
    _fake_git(tmp_path, monkeypatch,
              f"open({str(calls)!r}, 'a').write('x')\nprint('git version 2.40.1 (fake)')\n")
    first = git_host.capability()
    assert git_host.capability() is first
    assert first.state == "ok" and calls.read_text() == "x"
    git_host.reset_capability()
    git_host.capability()
    assert calls.read_text() == "xx"


def test_a_child_gets_the_hardened_environment_and_prefix(tmp_path, monkeypatch):
    """The fake git records its argv and environment: no ambient GIT_*,
    a temporary HOME under the ceiling, replace objects off, the -c prefix
    and the file-only protocol pin before --git-dir, the store-write flags
    only when asked."""
    record = tmp_path / "record.json"
    _fake_git(tmp_path, monkeypatch, (
        "import json, os, sys\n"
        "if sys.argv[1:] == ['--version']:\n    print('git version 2.45.0'); sys.exit(0)\n"
        f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd(),\n"
        f"           'stdin': sys.stdin.read()}}, open({str(record)!r}, 'w'))\n"))
    monkeypatch.setenv("GIT_TRACE", "1")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath=/x'")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    import json
    git_host.run(["index-pack", "--stdin"], git_dir="/s.git", input=b"pack", store_write=True)
    seen = json.loads(record.read_text())
    ceiling = os.path.realpath(".collimate/tmp")
    env = seen["env"]
    assert not {"GIT_TRACE", "GIT_CONFIG_PARAMETERS", "GITHUB_TOKEN"} & set(env)
    assert env["GIT_CEILING_DIRECTORIES"] == ceiling
    assert env["HOME"] == env["XDG_CONFIG_HOME"] == seen["cwd"]
    assert os.path.dirname(env["HOME"]) == ceiling and not os.path.exists(env["HOME"])
    assert (env["GIT_NO_REPLACE_OBJECTS"], env["LC_ALL"], env["GIT_CONFIG_NOSYSTEM"],
            env["GIT_CONFIG_GLOBAL"], env["GIT_TERMINAL_PROMPT"]) == ("1", "C", "1", "/dev/null", "0")
    argv = seen["argv"]
    from collimate.git_hosts import HARDENING_FLAGS
    assert argv[:len(HARDENING_FLAGS)] == list(HARDENING_FLAGS)
    rest = argv[len(HARDENING_FLAGS):]
    assert rest == ["-c", "protocol.allow=never", "-c", "protocol.file.allow=always", "--git-dir=/s.git",
                    *git_host.STORE_WRITE_FLAGS, "index-pack", "--stdin"]
    assert seen["stdin"] == "pack"
    git_host.run(["status"], git_dir="/r/.git", work_tree="/r")
    argv = json.loads(record.read_text())["argv"]
    assert argv[-3:] == ["--git-dir=/r/.git", "--work-tree=/r", "status"]
    assert not set(git_host.STORE_WRITE_FLAGS) - {"-c"} & set(argv)


def test_a_failure_carries_its_exit_and_the_tail_of_stderr(tmp_path, monkeypatch):
    _fake_git(tmp_path, monkeypatch, (
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n    print('git version 2.45.0'); sys.exit(0)\n"
        "sys.stderr.write('x' * 200000 + '\\nfatal: the last words\\n'); sys.exit(128)\n"))
    with pytest.raises(git_host.GitFailed) as failed:
        git_host.run(["fsck"])
    exc = failed.value
    assert exc.returncode == 128 and not exc.timed_out
    assert len(exc.stderr.encode()) == git_host.STDERR_CAP
    assert exc.stderr.endswith("fatal: the last words\n")
    assert str(exc) == "git fsck exited 128: fatal: the last words"
    assert git_host.run(["fsck"], ok_returncodes=(128,)).returncode == 128


def test_a_child_past_its_timeout_is_killed_and_reported(tmp_path, monkeypatch):
    _fake_git(tmp_path, monkeypatch, (
        "import sys, time\n"
        "if sys.argv[1:] == ['--version']:\n    print('git version 2.45.0'); sys.exit(0)\n"
        "time.sleep(30)\n"))
    with pytest.raises(git_host.GitFailed) as failed:
        git_host.run(["rev-list", "--all"], timeout=0.5)
    assert failed.value.timed_out and failed.value.returncode is None
    assert str(failed.value) == "git rev-list timed out"


def test_a_timeout_kills_the_processes_git_started_too(tmp_path, monkeypatch):
    """repack's pack-objects must not write into a store after its lock is gone."""
    marker = tmp_path / "late"
    _fake_git(tmp_path, monkeypatch, (
        "import os, sys, time\n"
        "if sys.argv[1:] == ['--version']:\n    print('git version 2.45.0'); sys.exit(0)\n"
        "if os.fork() == 0:\n"
        f"    time.sleep(1.5); open({str(marker)!r}, 'w').close(); os._exit(0)\n"
        "time.sleep(30)\n"))
    with pytest.raises(git_host.GitFailed) as failed:
        git_host.run(["-c", "repack.writeBitmaps=false", "repack", "-a", "-d"], timeout=0.5)
    assert str(failed.value) == "git repack timed out"
    time.sleep(2)
    assert not marker.exists()


@pytest.mark.parametrize(("command", "named"), [
    (["-c", "a.b=c", "-C", "/x", "--no-pager", "fsck"], "git fsck exited 1"),
    (["--version"], "git exited 1"),
    ([], "git exited 1"),
])
def test_a_failure_names_the_subcommand_past_its_options(command, named):
    assert str(git_host.GitFailed(command, 1, "")) == named


@GIT_OK
def test_real_git_sees_the_hardening_at_command_scope_and_no_ambient_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = Ambient\n")
    monkeypatch.setenv("HOME", str(home))
    out = git_host.run(["config", "--get", "core.hooksPath"]).stdout
    assert out == b"/dev/null\n"
    assert git_host.run(["config", "--get", "user.name"], ok_returncodes=(1,)).returncode == 1
    assert git_host.run(["hash-object", "--stdin"], input=b"x\n").stdout.strip() == \
        b"587be6b4c3f93f93c489c0111bba5596147a26cb"


@pytest.mark.parametrize("subcommand", ["subcommand", None])
def test_a_failure_keeps_its_subcommand(subcommand):
    assert git_host.GitFailed(["-c", "a=b", *([subcommand] if subcommand else [])], 1, "").subcommand == subcommand


@GIT_OK
def test_a_child_reads_stdin_from_a_file_and_writes_stdout_to_one(tmp_path):
    source, sink = tmp_path / "in", tmp_path / "out"
    source.write_bytes(b"x\n")
    with open(source, "rb") as stream, open(sink, "wb") as out:
        done = git_host.run(["hash-object", "--stdin"], input=stream, output=out)
    assert done.stdout == b""
    assert sink.read_bytes().strip() == b"587be6b4c3f93f93c489c0111bba5596147a26cb"


@GIT_OK
def test_no_repository_is_discovered_without_git_dir(tmp_path):
    """Run from the data dir with a repository above it: with no --git-dir,
    git sees no repository at all."""
    from git_repos import init
    init(tmp_path)
    done = git_host.run(["rev-parse", "--git-dir"], ok_returncodes=(128,))
    assert done.returncode == 128 and "not a git repository" in done.stderr
