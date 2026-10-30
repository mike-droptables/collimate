"""The host probe finds a harness CLI the machine already has, never
installs anything, and never caches a miss."""
import os
import stat
from pathlib import Path

import pytest

from collimate import host_harness
from collimate.account_taxonomy import HARNESS_HOST_BINARY, MODEL_HARNESS_RULES


@pytest.fixture(autouse=True)
def _no_machine_roots(monkeypatch):
    """Keep the probe inside the test's tmp_path. ``home=`` only relocates the
    ``~`` roots; the absolute ones (/snap/bin, /usr/local/bin, Homebrew) are
    the developer's real machine, where an installed codex or goose would win
    the search, flip these assertions and get executed for its --version."""
    monkeypatch.setattr(host_harness, "_KNOWN_ROOTS",
                        tuple(r for r in host_harness._KNOWN_ROOTS if r.startswith("~")))
    host_harness.forget()
    yield
    host_harness.forget()


def _fake(dir: Path, name: str, version: str = "1.2.3", answers: bool = True) -> Path:
    dir.mkdir(parents=True, exist_ok=True)
    path = dir / name
    path.write_text(f"#!/bin/sh\n{'echo ' + name + ' ' + version if answers else 'exit 3'}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_every_harness_but_the_engine_has_a_host_binary():
    assert set(HARNESS_HOST_BINARY) == set(MODEL_HARNESS_RULES) - {"pydantic-ai"}
    assert HARNESS_HOST_BINARY["antigravity"] == "agy"


def test_probe_finds_the_cli_on_path_and_reads_its_version(tmp_path):
    bin_dir = tmp_path / "bin"
    _fake(bin_dir, "codex", "0.114.0")
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    found = host_harness.probe("codex", env=env, home=str(tmp_path))
    assert found and found.path == str(bin_dir / "codex") and found.version == "0.114.0" and found.usable
    assert "codex 0.114.0 at" in found.describe()
    assert host_harness.probe("goose", env=env, home=str(tmp_path)) is None


def test_probe_looks_past_path_in_the_usual_roots_and_flags_snap(tmp_path):
    _fake(tmp_path / ".local" / "bin", "opencode", "1.18.32")
    env = {"PATH": "/nonexistent", "HOME": str(tmp_path)}
    found = host_harness.probe("opencode", env=env, home=str(tmp_path))
    assert found and found.path == str(tmp_path / ".local" / "bin" / "opencode") and not found.is_snap
    assert str(tmp_path / ".local" / "bin") in found.searched


def test_a_binary_that_does_not_answer_is_found_but_not_usable(tmp_path):
    bin_dir = tmp_path / "bin"
    _fake(bin_dir, "pi", answers=False)
    found = host_harness.probe("pi", env={"PATH": str(bin_dir)}, home=str(tmp_path))
    assert found and not found.usable and "did not answer --version" in found.describe()


def test_only_agy_counts_as_antigravity(tmp_path):
    bin_dir = tmp_path / "bin"
    _fake(bin_dir, "antigravity", "1.107.0")
    env = {"PATH": str(bin_dir)}
    assert host_harness.probe("antigravity", env=env, home=str(tmp_path)) is None
    _fake(bin_dir, "agy", "1.2.8")
    assert host_harness.probe("antigravity", env=env, home=str(tmp_path)).version == "1.2.8"


def test_a_miss_is_never_cached_but_a_hit_is(tmp_path, monkeypatch):
    host_harness.forget()
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
    assert host_harness.probe("goose") is None
    _fake(bin_dir, "goose", "1.51.0")
    assert host_harness.probe("goose").version == "1.51.0"
    (bin_dir / "goose").unlink()
    assert host_harness.probe("goose").version == "1.51.0", "a hit is cached for the process"
    host_harness.forget()
    assert host_harness.probe("goose") is None


def test_a_usable_copy_further_on_beats_a_snap_or_a_silent_one_first_on_path(tmp_path):
    # A path with "/snap/" in it reads as a confined Snap (the real /snap/bin
    # case, e.g. a third-party codex Snap ahead of an npm install).
    snap_bin = tmp_path / "snap" / "codex" / "bin"
    _fake(snap_bin, "codex", "0.114.0")
    silent = tmp_path / "silent"
    _fake(silent, "codex", answers=False)
    good = tmp_path / "good"
    _fake(good, "codex", "0.130.0")
    env = {"PATH": os.pathsep.join([str(snap_bin), str(silent), str(good)])}
    found = host_harness.probe("codex", env=env, home=str(tmp_path))
    assert found and found.usable and found.path == str(good / "codex") and found.version == "0.130.0"


def test_a_blocked_copy_is_still_the_answer_when_nothing_usable_exists(tmp_path):
    snap_bin = tmp_path / "snap" / "codex" / "bin"
    _fake(snap_bin, "codex", "0.114.0")
    found = host_harness.probe("codex", env={"PATH": str(snap_bin)}, home=str(tmp_path))
    assert found and found.is_snap and not found.usable
    assert "strictly confined Snap" in found.blocker


def test_one_binary_reached_through_two_dirs_is_probed_once(tmp_path, monkeypatch):
    # A SILENT binary reached through a symlinked dir and its real dir, then a
    # good copy: the search passes both names of the silent one, and asks it
    # --version only once.
    real_dir = tmp_path / "real"
    _fake(real_dir, "goose", answers=False)
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    good = tmp_path / "good"
    _fake(good, "goose", "1.51.0")
    calls = []
    original = host_harness._version_of
    monkeypatch.setattr(host_harness, "_version_of", lambda path, env: calls.append(path) or original(path, env))
    env = {"PATH": os.pathsep.join([str(link_dir), str(real_dir), str(good)])}
    found = host_harness.probe("goose", env=env, home=str(tmp_path))
    assert found and found.usable and found.path == str(good / "goose")
    assert calls == [str(link_dir / "goose"), str(good / "goose")]


def test_a_blocked_copy_is_not_cached_so_a_fresh_install_is_seen(tmp_path, monkeypatch):
    # Verify runs in the long-lived API process: after it reports a Snap and
    # the user installs a usable copy, the next Verify must find that copy.
    host_harness.forget()
    snap_bin = tmp_path / "snap" / "codex" / "bin"
    _fake(snap_bin, "codex", "0.114.0")
    good = tmp_path / "good"
    monkeypatch.setenv("PATH", os.pathsep.join([str(snap_bin), str(good)]))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
    assert host_harness.probe("codex").is_snap
    _fake(good, "codex", "0.156.1")
    found = host_harness.probe("codex")
    assert found.usable and found.path == str(good / "codex")
    host_harness.forget()


def test_a_snap_is_recognised_through_any_path_to_the_snap_launcher(tmp_path):
    # /snap/bin/<cmd> -> /usr/bin/snap: a link of the user's own (or the
    # /var/lib/snapd/snap/bin PATH some distros use) is still a Snap.
    launcher_dir = tmp_path / "usr" / "bin"
    _fake(launcher_dir, "snap", "2.63")
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "codex").symlink_to(launcher_dir / "snap")
    found = host_harness.probe("codex", env={"PATH": str(mine)}, home=str(tmp_path))
    assert found and found.is_snap and not found.usable


def test_the_suite_never_reaches_the_machine_s_own_install_roots(tmp_path):
    # Every harness binary missing from PATH and the tmp home must be a miss
    # on ANY machine — a hit here means a real /snap/bin or /usr/local/bin CLI
    # leaked into the unit suite (and was run for its --version).
    for harness in HARNESS_HOST_BINARY:
        assert host_harness.probe(harness, env={"PATH": str(tmp_path / "none")},
                                  home=str(tmp_path)) is None, harness


def test_version_manager_roots_are_searched_newest_node_first(tmp_path):
    node = tmp_path / ".nvm" / "versions" / "node"
    for folder, version in (("v9.0.0", "0.1.0"), ("v20.9.0", "0.2.0"), ("v20.11.0", "0.3.0"), ("v22.1.0", "0.4.0")):
        _fake(node / folder / "bin", "codex", version)
    found = host_harness.probe("codex", env={"PATH": "/nonexistent"}, home=str(tmp_path))
    assert found and found.path == str(node / "v22.1.0" / "bin" / "codex") and found.version == "0.4.0"
    order = list(found.searched)
    assert order.index(str(node / "v20.11.0" / "bin")) < order.index(str(node / "v20.9.0" / "bin"))
    assert order.index(str(node / "v20.9.0" / "bin")) < order.index(str(node / "v9.0.0" / "bin"))


@pytest.mark.parametrize("kind", ("missing", "blocked", "usable"))
def test_probe_scope_caches_every_result_then_reprobes_F1625_F1378(tmp_path, monkeypatch, kind):
    bin_dir = tmp_path / "bin"
    if kind != "missing":
        _fake(bin_dir, "codex", answers=kind == "usable")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
    calls = []
    original = host_harness._candidates
    monkeypatch.setattr(host_harness, "_candidates", lambda env, home: calls.append(True) or original(env, home))
    with host_harness.probe_scope():
        first = host_harness.probe("codex")
        assert host_harness.probe("codex") is first
        with host_harness.probe_scope():
            assert host_harness.probe("codex") is first
    assert len(calls) == 1
    with host_harness.probe_scope():
        host_harness.probe("codex")
    assert len(calls) == 2
