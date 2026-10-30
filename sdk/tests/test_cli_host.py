# SPDX-License-Identifier: MIT
"""The host side of a CLI leg (``_agent_shared.cli_host``): the per-account
home's location and permissions, its persistence across cells, the leg's
own environment, the machine's CLI, and the cell links a leg makes."""
from __future__ import annotations

import os
import stat

import pytest

from _agent_shared import cli_host


@pytest.fixture
def cache(monkeypatch, tmp_path):
    root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(root))
    return root


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_the_home_is_per_harness_and_entry_under_the_host_cache_owner_only(cache):
    home = cli_host.account_home("claude-code", {"id": "model-1", "credential_account_id": "entry-1"})
    assert home == str(cache / "collimate" / "host-homes" / "claude-code" / "entry-1")
    level = home
    while level != str(cache):
        assert os.path.isdir(level) and _mode(level) == 0o700, level
        level = os.path.dirname(level)


def test_one_sign_in_per_key_manager_entry(cache):
    """The model entries under one credential account share its home; the
    entry itself is the key when it has no parent; another harness or entry
    gets its own."""
    entry = cli_host.account_home("antigravity", {"id": "fast", "credential_account_id": "google-1"})
    assert cli_host.account_home("antigravity", {"id": "slow", "credential_account_id": "google-1"}) == entry
    assert cli_host.account_home("antigravity", {"id": "google-1"}) == entry
    assert cli_host.account_home("antigravity", {"id": "google-2"}) != entry
    assert cli_host.account_home("claude-code", {"id": "google-1"}) != entry


def test_the_home_outlives_cells_and_runs(cache, monkeypatch):
    """Nothing in the home is keyed by the cell or the run, and nothing
    removes it: a sign-in written there in one cell is there in the next."""
    import gen
    for cell in ("cell-a", "cell-b"):
        monkeypatch.setattr(gen, "run_info", lambda cell=cell: type("I", (), {"cell_id": cell, "run_id": cell})())
        home = cli_host.account_home("claude-code", {"id": "entry-1"})
        if cell == "cell-a":
            with open(os.path.join(home, ".credentials.json"), "w") as fh:
                fh.write("{}")
    assert os.path.exists(os.path.join(home, ".credentials.json"))


@pytest.mark.parametrize("identity", ["../escape", "a/b", ".", "", "x" * 300])
def test_an_id_that_is_not_a_plain_name_is_hashed_or_refused(cache, identity):
    if not identity:
        with pytest.raises(ValueError, match="no id"):
            cli_host.account_home("claude-code", {"id": identity})
        return
    key = cli_host.account_key({"id": identity})
    assert key.startswith("sha256-") and "/" not in key
    home = cli_host.account_home("claude-code", {"id": identity})
    assert os.path.dirname(home) == str(cache / "collimate" / "host-homes" / "claude-code")


def test_the_selected_model_account_is_the_default(cache, monkeypatch):
    import gen
    monkeypatch.setattr(gen, "account", lambda role: {"id": "selected-entry"})
    assert cli_host.account_home("claude-code").endswith("/claude-code/selected-entry")


def test_the_leg_environment_replaces_the_childs(monkeypatch):
    """PATH, HOME, an xterm TERM, the locale and the leg's own values:
    nothing else of this process's environment — no other account's key,
    no server variable — ever reaches the CLI."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-this-accounts")
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-gemini")
    monkeypatch.setenv("COLLIMATE_IPC_TOKEN", "server-secret")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    for name in ("LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(name, raising=False)
    env = cli_host.leg_env(home="/accounts/home", CLAUDE_CONFIG_DIR="/accounts/home", DROPPED=None)
    assert set(env) == {"PATH", "HOME", "TERM", "LANG", "CLAUDE_CONFIG_DIR"}
    assert env["HOME"] == "/accounts/home" and env["TERM"] == "xterm-256color"
    assert env["LANG"] == "en_US.UTF-8" and env["PATH"]
    assert cli_host.leg_env()["HOME"] == os.environ["HOME"]


def test_the_cli_is_the_machines_own_copy():
    assert cli_host.cli_binary("claude-code", {"path": "/opt/bin/claude"}) == "/opt/bin/claude"
    for found in (None, {}, {"path": "claude"}):
        with pytest.raises(RuntimeError, match="this machine's claude, which was not found"):
            cli_host.cli_binary("claude-code", found)


def test_a_cell_link_points_a_home_folder_at_the_cells_and_goes_with_the_leg(tmp_path):
    home, cell = tmp_path / "home", tmp_path / "cell" / "projects" / "-ws"
    link = cli_host.link_cell_dir(str(home), "projects/-ws", str(cell))
    assert os.readlink(link) == str(cell) and _mode(cell) == 0o700
    # A later leg replaces its own stale link.
    assert cli_host.link_cell_dir(str(home), "projects/-ws", str(cell)) == link
    cli_host.unlink_cell_dir(link, str(cell))
    assert not os.path.lexists(link)
    # A real folder of the CLI's own in its place is kept, and no link made.
    os.makedirs(home / "projects" / "-own")
    assert cli_host.link_cell_dir(str(home), "projects/-own", str(cell)) == ""
    assert os.path.isdir(home / "projects" / "-own") and not os.path.islink(home / "projects" / "-own")


def test_dangling_links_are_swept_and_live_ones_kept(tmp_path):
    folder = tmp_path / "projects"
    folder.mkdir()
    live, gone = tmp_path / "live", tmp_path / "gone"
    live.mkdir()
    os.symlink(live, folder / "live")
    os.symlink(gone, folder / "gone")
    (folder / "real").mkdir()
    cli_host.sweep_dangling_links(str(folder))
    assert sorted(os.listdir(folder)) == ["live", "real"]
    cli_host.sweep_dangling_links(str(tmp_path / "missing"))
