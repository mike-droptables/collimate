# SPDX-License-Identifier: MIT
"""Tests for the ``launch`` generator (the one runner behind oneshot /
terminal / app) and its config templates.

Docker is stubbed the way the ``_env_shared`` tests stub it — we monkeypatch
the module's ``DockerEnvironment`` with a recording fake, so the mode logic
(image resolution, scripts ordering, result pull-back, serve, the UI files in
the run folder) is exercised without a Docker daemon. The
real-``DockerEnvironment`` paths are covered by the degrade-cleanly-without-host
tests (no docker package ⇒ clean error surface).
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import sys
import types

import pytest

import gen  # the top-level shim; bridged by the harness during a run

from collimate.lint.core import Level, lint_path
from collimate.testing import run_generator, run_generator_callable as _run_generator_callable

from tests._workspace import GENERATORS

from _env_shared import git_account  # before any run, so its patches survive

from tests._git_fixtures import bind_account, git_account_snapshot


_RESULT_UPDATES = {}


def run_generator_callable(*args, **kwargs):
    from unittest.mock import patch
    import collimate.testing as testing
    made = []
    original = testing.make_test_bridge

    def record(*args, **kwargs):
        bridge = original(*args, **kwargs)
        made.append(bridge)
        return bridge

    with patch.object(testing, "make_test_bridge", record):
        result = _run_generator_callable(*args, **kwargs)
    _RESULT_UPDATES[id(result)] = made[-1].updates if made else []
    return result


_ROOT = os.path.dirname(os.path.dirname(__file__))
_GEN_DIR = str(GENERATORS)
_RUN_PY = os.path.join(_GEN_DIR, "launch", "launch", "run.py")


# ---------------------------------------------------------------------------
# Recording Docker fake
# ---------------------------------------------------------------------------


class _FakeContainer:
    def __init__(self, calls=None):
        self.name = "fake-container"
        self.attrs = {"NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]}}}
        self.calls = calls if calls is not None else []

    def exec_run(self, cmd, **k):
        self.calls.append({"cmd": cmd, **k})
        return types.SimpleNamespace(exit_code=0, output=b"OK")

    def logs(self, *a, **k):
        return iter(())


# The container's uid and gid maps as the owner probe prints them: the
# identity on rootful Docker, the host user as root (and its subordinate ids
# after it) under rootless Docker, only subordinate ids under userns-remap.
def _maps(uid_map: str, gid_map: str = "") -> str:
    return f"{uid_map}\n--\n{gid_map or uid_map}\n"


_IDENTITY = _maps("0 0 4294967295")
_ROOTLESS = _maps(f"0 {os.getuid()} 1\n1 100000 65536", f"0 {os.getgid()} 1\n1 100000 65536")
_REMAPPED = _maps("0 100000 65536")
_OWNER_OUT = _IDENTITY
# The workspace owner a hand-back targets when the maps are the identity:
# the test user, who made the run's workspace.
_ME = f"{os.getuid()}:{os.getgid()}"


def _make_fake(records, exec_writes=None, exec_output="ran", exec_hook=None,
               exec_rc=0, passes=(), on_handback=None, owner_out=_OWNER_OUT):
    """A DockerEnvironment stand-in recording constructor kwargs + exec calls.

    ``exec_writes`` (``{relpath: content}``) is written straight into the cell
    workspace on the first ``exec`` — simulating a container writing results to
    the bind-mounted workspace, which the generator must then pull back into the
    cell explicitly. ``passes[i](workspace)`` runs on the i-th command exec.
    An exec as root (``user="0:0"``) is the probe of the container's id maps
    (recorded in ``probes``, answered with ``owner_out``, or with its items
    in turn when it is a list) or the workspace hand-back (recorded in
    ``handbacks``; ``on_handback(workspace)`` plays the container's part),
    never in ``execs``, which holds the commands the runner's settings
    name. ``stop_process`` (followed by "wait" when asked to wait for the
    process to exit) and ``freeze`` are recorded in the timeline."""

    class _Fake:
        def __init__(self, **kw):
            base = kw.get("base_context") or (
                kw.get("build_context") if kw.get("image", "").startswith("collimate-linux-small:") else None)
            if base:
                with open(os.path.join(base, "Dockerfile"), encoding="utf-8") as handle:
                    kw["staged_base_dockerfile"] = handle.read()
            kw["execs"] = []
            kw["bg"] = []
            kw["probes"] = []
            kw["handbacks"] = []
            kw["stopped"] = []
            kw["timeline"] = []
            kw["container_calls"] = []
            records.append(kw)
            self._rec = kw
            self._wrote = False
            self.container = _FakeContainer(kw["container_calls"])
            self.container_id = "fake-id"

        def exec(self, command, *, cwd=None, env=None, timeout=None, user=None):
            base = os.path.abspath(gen.workspace_path())
            if user == "0:0" and "/proc/self/uid_map" in command[-1]:
                self._rec["probes"].append(command)
                self._rec["timeline"].append("owner")
                outs = owner_out if isinstance(owner_out, list) else [owner_out]
                out = outs[min(len(self._rec["probes"]), len(outs)) - 1]
                return out if isinstance(out, tuple) else (0, out)
            if user == "0:0":
                self._rec["handbacks"].append(command)
                self._rec["timeline"].append("handback")
                if on_handback:
                    on_handback(base)
                return (0, "")
            if user in ("root", "coder"):
                result = self.container.exec_run(command, user=user, environment=env)
                return result.exit_code, result.output.decode("utf-8", errors="replace")
            self._rec["execs"].append(command)
            self._rec["timeline"].append("exec")
            n = len(self._rec["execs"])
            if n <= len(passes):
                passes[n - 1](base)
            if exec_writes and not self._wrote:
                self._wrote = True
                for rel, content in exec_writes.items():
                    full = os.path.join(base, rel)
                    os.makedirs(os.path.dirname(full) or base, exist_ok=True)
                    with open(full, "w", encoding="utf-8") as fh:
                        fh.write(content)
                if exec_hook:
                    exec_hook(base)
            return (exec_rc, exec_output)

        def exec_background(self, command, *, cwd=None, env=None):
            self._rec["bg"].append(command)
            return "proc-1"

        def stop_process(self, process_id, *, wait=False):
            self._rec["stopped"].append(process_id)
            self._rec["timeline"].append("stop")
            if wait:
                # The real one then polls until the process group has
                # exited, or its stop grace has passed.
                self._rec["timeline"].append("wait")

        def freeze(self):
            self._rec["timeline"].append("freeze")

        async def expose_port(self, port):
            return types.SimpleNamespace(url=f"/proxy/{port}/", port=port, kind="http", name="p")

        def close(self):
            self._rec["closed"] = True
            self._rec["timeline"].append("close")

    return _Fake


def _load_run_module():
    if _GEN_DIR not in sys.path:
        sys.path.insert(0, _GEN_DIR)
    spec = importlib.util.spec_from_file_location("_run_gen_under_test", _RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A launch run's identity and the UI file types its manifest declares, as a
# hosted run gets them.
_LABEL = "launch-0123abcd"
_FOLDER = f"_runs/{_LABEL}"
_UI_FILES = [".terminal", ".website"]


def _run_oneshot(config, *, records=None, config_folders=None, exec_writes=None,
                 prompt="", timeout=20, exec_hook=None, exec_rc=0,
                 exec_output="ran", passes=(), on_handback=None,
                 owner_out=_OWNER_OUT, patch=None, **kw):
    """Run the generator with DockerEnvironment faked; return (result,
    records). ``patch(module)`` adjusts the loaded generator module first."""
    records = records if records is not None else []
    mod = _load_run_module()
    mod.DockerEnvironment = _make_fake(records, exec_writes=exec_writes,
                                       exec_hook=exec_hook, exec_rc=exec_rc,
                                       exec_output=exec_output, passes=passes,
                                       on_handback=on_handback, owner_out=owner_out)
    if patch:
        patch(mod)
    res = run_generator_callable(
        mod.run, config={"settings.set_yaml": config},
        config_folders=config_folders, prompt=prompt, timeout=timeout,
        label=_LABEL, ui_files=_UI_FILES, **kw,
    )
    return res, records


def _flushed(res, path):
    """Every flushed version of ``path``, in order."""
    return [c["files"][path] for c in _RESULT_UPDATES[id(res)] if path in c["files"]]


def _reveals(res):
    return [(c["path"], c["committed"]) for c in res.tier_b_calls
            if c["op"] == "reveal_file"]


def _stop_after(bridge_hook, clicks=()):
    """Script the run's toolbar presses: ``clicks`` (action events), then
    Stop. A wait takes the first queued press its ``source`` / ``action_id``
    filters match, as the host's keyed queue does; one that matches none of
    them is never delivered."""
    def setup(bridge):
        bridge._autocancel_on_serve = False
        queued = list(clicks)

        def poll(*, source="", action_id=""):
            for i, ev in enumerate(queued):
                if ((not source or ev["source"] == source)
                        and (not action_id or ev["action_id"] == action_id)):
                    return queued.pop(i)
            bridge._signal_stop()   # the Stop button ends the serve loop
            return None

        bridge.poll_renderer_action_event = poll

    bridge_hook["setup"] = setup


# A press on the serve arm's preview toolbar Re-run.
_RERUN = {"action_id": "rerun", "source": f"{_FOLDER}/preview.website"}


# ---------------------------------------------------------------------------
# Image resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["oneshot", "terminal", "app"])
@pytest.mark.parametrize("runner_name", ["My launch recipe", ""])
def test_launch_proposes_runner_name_in_each_mode__F1645_F1653(mode, runner_name):
    def prepare(mod):
        async def ready(port, timeout=120):
            return True
        mod._wait_for_ready = ready
    result, records = _run_oneshot(
        {"mode": mode, "command": "echo hi"},
        runner={"name": runner_name}, patch=prepare,
    )
    assert result.ok, result.error
    assert records, "the selected launch mode did not reach its environment"
    kind = {"oneshot": "launch", "terminal": "launch_terminal", "app": "launch_app"}[mode]
    assert records[-1]["kind"] == kind
    assert records[-1]["name_prefix"].startswith("collimate-launch")
    names = [update["name"] for update in _RESULT_UPDATES[id(result)]
             if update["name"] is not None]
    assert names == [runner_name or "Launch"]


def test_image_resolution_base_fallback():
    # No image, no build folder → the shared base image, built from _env_shared,
    # tagged by the content of that context (never a fixed :v3).
    res, rec = _run_oneshot({"mode": "oneshot", "command": "echo hi"})
    assert res.ok, res.error
    assert rec, "DockerEnvironment was never constructed"
    call = rec[-1]
    repo, _, digest = call["image"].partition(":")
    assert repo == "collimate-linux-small"
    assert len(digest) == 12 and digest.isalnum()
    assert call["build_context"] and call["build_context"].endswith(
        os.path.join(".collrun", "container-base"))
    assert call["base_image"] is None
    with open(os.path.join(_GEN_DIR, "_env_shared", "base", "Dockerfile"), encoding="utf-8") as handle:
        assert call["staged_base_dockerfile"] == handle.read()


def test_image_resolution_setting_wins():
    # A bare `image` setting (no build folder) → that tag, used as-is (no build).
    # The cell doesn't define this image's content, so there is nothing to hash.
    res, rec = _run_oneshot({"mode": "oneshot", "command": "echo hi", "image": "my/img:2"})
    assert res.ok, res.error
    call = rec[-1]
    assert call["image"] == "my/img:2"
    assert call["build_context"] is None
    assert call["base_image"] is None


def test_image_resolution_build_folder_derives_tag_and_ensures_base():
    # A build folder whose Dockerfile takes the shared base (ARG BASE default +
    # FROM ${BASE} — the base-detection grep matches the ARG line) → a
    # content-derived tag, the build context, the content-addressed base ensured
    # first, and that base injected as the BASE build arg so FROM ${BASE}
    # resolves to it instead of the Dockerfile's literal default.
    dockerfile = "ARG BASE=collimate-linux-small:v3\nFROM ${BASE}\nRUN echo hi\n"
    res, rec = _run_oneshot(
        {"mode": "oneshot", "command": "echo hi"},
        config_folders={"build": {"Dockerfile": dockerfile}},
    )
    assert res.ok, res.error
    call = rec[-1]
    assert call["image"].startswith("collimate-launch-build:")
    assert call["build_context"].endswith(os.path.join("folders", "build"))
    assert call["base_image"].startswith("collimate-linux-small:")
    assert call["base_image"] != "collimate-linux-small:v3"
    assert call["base_context"].endswith(os.path.join(".collrun", "container-base"))
    assert call["build_args"] == {"BASE": call["base_image"]}
    with open(os.path.join(_GEN_DIR, "_env_shared", "base", "Dockerfile"), encoding="utf-8") as handle:
        assert call["staged_base_dockerfile"] == handle.read()


def test_image_resolution_build_folder_no_base_when_from_is_public():
    # A build folder FROM a pullable public base → no local base is ensured.
    dockerfile = "FROM python:3.12-slim\nRUN echo hi\n"
    res, rec = _run_oneshot(
        {"mode": "oneshot", "command": "echo hi"},
        config_folders={"build": {"Dockerfile": dockerfile}},
    )
    assert res.ok, res.error
    call = rec[-1]
    assert call["base_image"] is None
    assert call["base_context"] is None
    assert call["build_args"] == {}


def test_image_resolution_build_folder_ignores_named_tag():
    # image setting + build folder → the CONTENT tag wins (the vscode pattern:
    # `image: collimate-vscode-env:v3` beside a build folder). A pinned tag
    # would make the host's tag-presence cache lie: the runner's Dockerfile edits
    # would never rebuild on a machine that already built that tag once.
    res, rec = _run_oneshot(
        {"mode": "oneshot", "command": "echo hi", "image": "collimate-vscode-env:v3"},
        config_folders={"build": {"Dockerfile": "FROM python:3.12-slim\n"}},
    )
    assert res.ok, res.error
    call = rec[-1]
    assert call["image"].startswith("collimate-launch-build:")
    assert call["build_context"].endswith(os.path.join("folders", "build"))


def test_image_tag_tracks_build_folder_content():
    # THE fix: identity is the content. Same folder → same tag (a cache hit, no
    # rebuild); one edited byte → a tag this host has never seen, hence a build.
    def _tag(dockerfile: str, extra: dict | None = None) -> str:
        files = {"Dockerfile": dockerfile, **(extra or {})}
        _res, rec = _run_oneshot(
            {"mode": "oneshot", "command": "echo hi"},
            config_folders={"build": files},
        )
        return rec[-1]["image"]

    first = _tag("FROM python:3.12-slim\nRUN pip install aider-chat==0.86.1\n")
    assert first == _tag("FROM python:3.12-slim\nRUN pip install aider-chat==0.86.1\n")
    assert first != _tag("FROM python:3.12-slim\nRUN pip install aider-chat==0.87.0\n")
    # A sibling file the Dockerfile COPYs is part of the identity too.
    assert first != _tag(
        "FROM python:3.12-slim\nRUN pip install aider-chat==0.86.1\n",
        {"entrypoint.sh": "#!/bin/sh\nexec \"$@\"\n"},
    )


# ---------------------------------------------------------------------------
# oneshot behaviour — command, scripts ordering, pull-back, serve
# ---------------------------------------------------------------------------


def test_oneshot_runs_command_and_pulls_results():
    res, rec = _run_oneshot(
        {"mode": "oneshot", "command": "python main.py"},
        exec_writes={"generated.txt": "made by the container"},
    )
    assert res.ok, res.error
    assert rec[-1]["execs"], "the command was never exec'd"
    assert "python main.py" in rec[-1]["execs"][0]
    # run.txt records the command + rc; the container-written file is pulled back.
    assert "python main.py" in res.files.get("run.txt", "")
    assert res.files.get("generated.txt") == "made by the container"
    assert "exit 0" in res.files.get("run.txt", "")


def test_oneshot_skips_symlinks_the_command_creates(tmp_path):
    # `npm install` / `python -m venv` / `ln -s` leave links in the workspace.
    # The pass stages the real files and skips the links (never cell content;
    # staging one hits the host's no-follow write) — without failing the run
    # or pulling a host file in through an absolute link.
    secret = tmp_path / "host_secret"
    secret.write_text("TOP SECRET")

    def plant(base):
        os.symlink("../tool/cli.js", os.path.join(base, "vendor", "bin", "tool"))
        os.symlink(str(secret), os.path.join(base, "leak.txt"))

    def writes_then_links(base):
        os.makedirs(os.path.join(base, "vendor", "bin"), exist_ok=True)
        plant(base)

    res, _ = _run_oneshot(
        {"mode": "oneshot", "command": "npm install"},
        exec_writes={"vendor/tool/cli.js": "#!/usr/bin/env node\n"},
        exec_hook=writes_then_links, timeout=30,
    )
    assert res.ok, res.error
    assert _flushed(res, "vendor/tool/cli.js") == ["#!/usr/bin/env node\n"]
    assert not any("vendor/bin/tool" in c["files"] for c in _RESULT_UPDATES[id(res)])
    assert "leak.txt" not in res.files
    assert all("TOP SECRET" not in str(v) for v in res.files.values())


def _cell_deletions(bridge_hook):
    """Record every ``deleted_files`` list the run flushes (the harness's
    cell records keep only the files written)."""
    seen: list = []
    prior = bridge_hook.get("setup")

    def setup(bridge):
        if prior:
            prior(bridge)
        inner = bridge._commit_update

        def update_cell(*, name=None, writes=None, deleted_files=None):
            seen.extend(deleted_files or ())
            return inner(name=name, writes=writes, deleted_files=deleted_files)

        bridge._commit_update = update_cell

    bridge_hook["setup"] = setup
    return seen


def test_oneshot_hands_files_back_before_pulling_them__F1638_F1966():
    """On rootful Docker a command's new files are root-owned: the host's
    pull-back cannot rewrite them (EACCES) until the workspace is handed
    back. The fake container writes read-only files (what root-owned looks
    like to the host user) and the hand-back makes them the user's again,
    so the file lands only when the hand-back runs between the command and
    the pull-back. No server follows the pass, so what the command left
    running is frozen before its hand-back."""
    if os.getuid() == 0:
        pytest.skip("root writes through any mode bit: nothing to simulate")

    def root_writes(base):
        with open(os.path.join(base, "made.txt"), "w", encoding="utf-8") as fh:
            fh.write("hi")
        os.chmod(os.path.join(base, "made.txt"), 0o444)

    def chown_back(base):
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                os.chmod(os.path.join(dirpath, fn), 0o644)

    res, rec = _run_oneshot(
        {"mode": "oneshot", "command": "echo hi > made.txt"},
        passes=[root_writes], on_handback=chown_back,
    )
    assert res.ok, res.error
    assert _flushed(res, "made.txt") == ["hi"]
    assert not any(line.startswith("skipped made.txt") for line in res.logs)
    assert rec[-1]["timeline"] == ["owner", "exec", "freeze", "handback", "close"]
    [probe] = rec[-1]["probes"]
    assert probe[:2] == ["sh", "-c"]
    assert "/proc/self/uid_map" in probe[2] and "/proc/self/gid_map" in probe[2]
    [script] = rec[-1]["handbacks"]
    assert script[:2] == ["sh", "-c"] and f"chown -h {_ME}" in script[2]
    assert "/work" in script[2] and "-prune" not in script[2]   # the last: all of it


def _owner_seen_at(records, owner):
    """A ``patch`` for :func:`_run_oneshot` whose host-owner read records
    how many containers were placed when it ran and answers ``owner``."""
    seen: list = []

    def patch(mod):
        def host_owner(path):
            assert os.path.samefile(path, gen.workspace_path())
            seen.append(len(records))
            return owner
        mod._host_owner = host_owner
    return seen, patch


def test_handback_targets_the_hosts_owner_read_before_placement__F1638_F1966(bridge_hook):
    """A root command can re-own the mount root itself (Git Clone's
    `cp -a /tmp/clone/. .` copies the clone's root:root onto /work), and so
    can an image's entrypoint before anything else runs. The owner every
    hand-back of the run targets (each pass, and the teardown after the
    server) is therefore the host's view of the workspace root, read
    before the container is placed, named in the container's ids through
    its id maps, which nothing in the container can change: the host ids
    themselves on rootful Docker, root under rootless Docker."""
    for maps, want in ((_IDENTITY, "4242:4343"), (_maps("0 4242 1", "0 4343 1"), "0:0")):
        records: list = []
        seen, patch = _owner_seen_at(records, (4242, 4343))
        _stop_after(bridge_hook, [_RERUN])
        res, rec = _run_oneshot(
            {"mode": "oneshot", "command": "cp -a /tmp/clone/. .",
             "serve_command": "python -m http.server 8000", "serve_port": 8000},
            records=records, patch=patch, owner_out=[maps, _REMAPPED], timeout=15,
        )
        assert res.ok, res.error
        assert seen == [0], "the host owner was read after the container was placed"
        assert rec[-1]["timeline"].count("owner") == 1
        assert rec[-1]["timeline"][0] == "owner"
        scripts = [h[2] for h in rec[-1]["handbacks"]]
        assert len(scripts) == 3
        assert all(f"chown -h {want}" in sc for sc in scripts), (want, scripts)


def test_oneshot_hands_back_after_every_pass_and_after_stopping_the_server__F1638(bridge_hook):
    """The serve arm re-runs the command in the same container and its
    server keeps writing after the last pass: each pass hands the
    workspace back, and the teardown stops the server, waits for it to
    exit (a SIGTERM handler rewriting a file finishes rather than being
    frozen halfway, F1966), freezes every other process, then hands back
    what they wrote, before the container goes."""
    _stop_after(bridge_hook, [_RERUN])
    res, rec = _run_oneshot(
        {"mode": "oneshot", "command": "echo built",
         "serve_command": "python -m http.server 8000", "serve_port": 8000},
        timeout=15,
    )
    assert res.ok, res.error
    assert rec[-1]["timeline"] == ["owner", "exec", "handback", "exec", "handback",
                                   "stop", "wait", "freeze", "handback", "close"]
    assert rec[-1]["stopped"] == ["proc-1"]
    # A pass walks the cell's content only (a re-run never pays for a
    # dependency tree's size); the last hand-back walks everything.
    first, second, last = (h[2] for h in rec[-1]["handbacks"])
    assert "-prune" in first and "-prune" in second and "-prune" not in last


def test_oneshot_teardown_goes_on_when_stopping_the_server_raises__F1966(bridge_hook):
    """Stopping the server raising (the host connection broke under its
    SIGTERM exec) skips none of the teardown after it: the freeze, the last
    hand-back and the close still run, and the log says why the server
    was not waited for."""
    _stop_after(bridge_hook)

    def broken_stop(mod):
        def stop_process(self, process_id, *, wait=False):
            self._rec["timeline"].append("stop")
            raise RuntimeError("host closed IPC channel while awaiting ack")
        mod.DockerEnvironment.stop_process = stop_process

    res, rec = _run_oneshot(
        {"mode": "oneshot", "command": "echo built",
         "serve_command": "python -m http.server 8000", "serve_port": 8000},
        patch=broken_stop,
    )
    assert res.ok, res.error
    assert rec[-1]["timeline"] == ["owner", "exec", "handback", "stop", "freeze", "handback",
                                   "close"]
    assert any("stopping the server failed: host closed IPC channel" in line
               for line in res.logs), res.logs


def test_oneshot_hands_back_in_teardown_when_a_pass_fails_mid_exec__F1638():
    """A pass whose exec raises (the host connection dropped) never reached
    its own hand-back: the teardown freezes what is still running and
    hands back whatever the command had already written."""
    def dropped(base):
        with open(os.path.join(base, "half.txt"), "w", encoding="utf-8") as fh:
            fh.write("half")
        raise RuntimeError("the exec's connection dropped")

    res, rec = _run_oneshot({"mode": "oneshot", "command": "make"}, passes=[dropped])
    assert not res.ok
    assert rec[-1]["timeline"] == ["owner", "exec", "freeze", "handback", "close"]


def test_oneshot_read_only_mount_is_not_handed_back__F1638():
    records: list = []
    seen, patch = _owner_seen_at(records, (4242, 4343))
    res, rec = _run_oneshot({"mode": "oneshot", "command": "ls", "mount_mode": "ro"},
                            records=records, patch=patch)
    assert res.ok, res.error
    assert seen == [] and rec[-1]["probes"] == [] and rec[-1]["handbacks"] == []
    assert "freeze" not in rec[-1]["timeline"]


def test_an_unmapped_or_unreadable_workspace_owner_hands_nothing_back__F1638():
    """Under userns-remap the host user is not mapped into the container: a
    chown to any id it has would give files to another host uid, so
    nothing is handed back. The same when the id maps cannot be read at
    all. Either way the log says so."""
    for out, said in ((_REMAPPED, "is not mapped into the container"),
                      ((1, "cat: /proc/self/uid_map: No such file"), "could not read"),
                      ((0, "0 0 4294967295\n"), "could not read"),
                      ("garbage\n--\ngarbage\n", "could not read")):
        res, rec = _run_oneshot({"mode": "oneshot", "command": "make"}, owner_out=out)
        assert res.ok, (out, res.error)
        assert rec[-1]["handbacks"] == [], out
        assert "freeze" not in rec[-1]["timeline"], out
        assert any(said in line for line in res.logs), (out, res.logs)


def test_an_id_map_range_holds_only_its_own_ids__F1638_P0F27(monkeypatch):
    """A map line ``<first inside> <first outside> <count>`` holds the host
    ids ``first outside`` up to, not including, ``first outside + count``.
    The id just past a range is not in it: under rootless Docker's
    ``0 1000 1`` the host uid 1001 is no container id, and taking it for
    container uid 1 would give the workspace to host subuid 100000."""
    mod = _load_run_module()
    rootless = ["0 1000 1", "1 100000 65536"]
    assert mod._map_in(rootless, 1000) == 0
    assert mod._map_in(rootless, 999) is None
    assert mod._map_in(rootless, 1001) is None
    assert mod._map_in(rootless, 100000) == 1
    assert mod._map_in(rootless, 165535) == 65536
    assert mod._map_in(rootless, 165536) is None

    logs: list = []
    monkeypatch.setattr(mod, "gen", types.SimpleNamespace(log=logs.append))

    class _Maps:
        def exec(self, argv, **kw):
            return 0, _maps("0 4242 1", "0 4343 1")

    assert mod._workspace_owner(_Maps(), (4242, 4343)) == "0:0"
    for host in ((4243, 4343), (4241, 4343), (4242, 4344), (4242, 4342)):
        assert mod._workspace_owner(_Maps(), host) is None, host
    assert len(logs) == 4 and all("is not mapped into the container" in line for line in logs)
    # A whole run: the host user one past the rootless map's own line is
    # handed nothing back.
    records: list = []
    seen, patch = _owner_seen_at(records, (4243, 4343))
    res, rec = _run_oneshot(
        {"mode": "oneshot", "command": "make"}, records=records, patch=patch,
        owner_out=_maps("0 4242 1\n1 100000 65536", "0 4343 1\n1 100000 65536"))
    assert res.ok, res.error
    assert rec[-1]["handbacks"] == [] and "freeze" not in rec[-1]["timeline"]


def test_an_unreadable_host_owner_hands_nothing_back__F1638(tmp_path, monkeypatch):
    logs: list = []
    mod = _load_run_module()
    monkeypatch.setattr(mod, "gen", types.SimpleNamespace(log=logs.append))
    assert mod._host_owner(str(tmp_path / "gone")) is None
    assert any("could not read the owner of the workspace" in line for line in logs)
    assert mod._host_owner(str(tmp_path)) == (os.getuid(), os.getgid())


def test_the_owner_probe_reads_the_containers_id_maps__F1638(tmp_path, monkeypatch):
    """The probe's real argv, run here as the container would run it: it
    prints this namespace's uid and gid maps, and the host owner of a
    directory (its stat, as the host reads it before placement) comes out
    as the id this namespace gives it — its own on an unnamespaced host."""
    import subprocess

    mod = _load_run_module()
    if not os.path.exists("/proc/self/uid_map"):
        pytest.skip("no user-namespace id maps on this system")
    monkeypatch.setattr(mod, "gen", types.SimpleNamespace(log=lambda *a, **k: None))

    class _Here:
        def __init__(self):
            self.argv = None

        def exec(self, argv, *, user=None, timeout=None, **kw):
            self.argv = argv
            assert user == "0:0"
            done = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            return done.returncode, done.stdout + done.stderr

    here = _Here()
    host = mod._host_owner(str(tmp_path))
    owner = mod._workspace_owner(here, host)
    assert "/proc/self/uid_map" in here.argv[-1] and "/proc/self/gid_map" in here.argv[-1]
    with open("/proc/self/uid_map", encoding="utf-8") as fh:
        identity = fh.read().split() == ["0", "0", "4294967295"]
    if identity:
        assert owner == _ME
    else:
        assert owner is None or re.fullmatch(r"\d+:\d+", owner)


def test_handback_script_chowns_only_foreign_entries_and_never_follows_links__F1638(tmp_path):
    """The hand-back gives entries, the mount root included, to the owner
    it is handed (read before the run), only where the owner or group
    differs, changing links themselves (``chown -h``) and never walking
    through a linked directory or into another mount (``-xdev``: a nested
    mount needs root to make, so that part is pinned on the script), and,
    pruning, never into or onto a skipped directory (a file of that name is
    no directory, and is handed back). Run for real against a directory,
    with ``chown`` shimmed on PATH (a non-root test cannot give files
    away)."""
    import subprocess

    mod = _load_run_module()
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "f.txt").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s")
    os.symlink(str(outside), ws / "dirlink")
    os.symlink("/etc/passwd", ws / "filelink")
    shims = tmp_path / "bin"
    shims.mkdir()
    calls = tmp_path / "chown.calls"
    (shims / "chown").write_text(f'#!/bin/sh\nprintf "%s\\n" "--call--" "$@" >> {calls}\n')
    os.chmod(shims / "chown", 0o755)

    def run_with_owner(owner: str, prune=frozenset()) -> list:
        if calls.exists():
            calls.unlink()
        script = mod._handback_script(str(ws), owner, prune=prune)
        assert "stat" not in script and " -xdev " in script
        env = {**os.environ, "PATH": f"{shims}:{os.environ['PATH']}"}
        subprocess.run(["sh", "-c", script], env=env, check=True, timeout=30)
        return calls.read_text().split("--call--\n")[1:] if calls.exists() else []

    # Every entry here is owned by someone other than 424242:434343 — the
    # mount root too, as after a root `cp -a src/. .` re-owned it.
    invocations = run_with_owner("424242:434343")
    assert invocations, "nothing was handed back"
    touched = set()
    for inv in invocations:
        args = inv.splitlines()
        assert args[:2] == ["-h", "424242:434343"], args
        touched.update(args[2:])
    assert touched == {str(ws), str(ws / "sub"), str(ws / "sub" / "f.txt"),
                       str(ws / "dirlink"), str(ws / "filelink")}
    # Pruning the live-files skip dirs: neither node_modules nor anything in
    # it, but a file named .git (a worktree's) is handed back.
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "node_modules" / "pkg" / "index.js").write_text("x")
    (ws / "sub" / ".git").write_text("gitdir: ../.git/worktrees/sub")
    touched = set()
    for inv in run_with_owner("424242:434343", prune=mod.WORKSPACE_SKIP_DIRS):
        touched.update(inv.splitlines()[2:])
    assert touched == {str(ws), str(ws / "sub"), str(ws / "sub" / "f.txt"),
                       str(ws / "sub" / ".git"), str(ws / "dirlink"), str(ws / "filelink")}
    # Already the owner's: nothing to change, so nothing is touched.
    assert run_with_owner(f"{os.getuid()}:{os.getgid()}") == []
    # A group mismatch alone is foreign too.
    assert run_with_owner(f"{os.getuid()}:{os.getgid() + 1}")


def test_oneshot_pull_back_skips_the_live_files_dirs__F1652_F1672():
    """Dependency trees, bytecode caches and VCS internals a command leaves
    in the workspace are not cell content — at any depth, the same set
    gen.live_files() skips."""
    res, _ = _run_oneshot(
        {"mode": "oneshot", "command": "python3 main.py"},
        exec_writes={
            "out.txt": "result",
            "__pycache__/helper.cpython-312.pyc": "bytecode",
            "pkg/__pycache__/mod.cpython-312.pyc": "bytecode",
            "node_modules/left-pad/index.js": "module.exports = 1",
            "web/node_modules/x/index.js": "module.exports = 2",
            ".venv/bin/activate": "# venv",
            ".git/HEAD": "ref: refs/heads/main",
        },
    )
    assert res.ok, res.error
    flushed = {p for c in _RESULT_UPDATES[id(res)] for p in c["files"]}
    assert "out.txt" in flushed
    assert not any(seg in p.split("/") for p in flushed
                   for seg in ("__pycache__", "node_modules", ".venv", ".git"))


def test_oneshot_skip_dirs_are_the_live_files_defaults__F1652_F1223(monkeypatch):
    import collimate.fs
    monkeypatch.setattr(collimate.fs, "WORKSPACE_SKIP_DIRS", frozenset({"private-cache"}))
    mod = _load_run_module()
    assert mod.WORKSPACE_SKIP_DIRS is collimate.fs.WORKSPACE_SKIP_DIRS
    assert mod._APP_SYNC_SKIP_DIRS == {"private-cache", ".vscode"}


def test_oneshot_rerun_drops_only_the_files_the_run_made__F1647(bridge_hook):
    """A re-run whose command deletes a file an earlier pass of this run
    made drops it from the cell too; an input the command deletes stays in
    the cell (the run never deletes what it did not make)."""
    def first(base):
        os.makedirs(os.path.join(base, "dist"), exist_ok=True)
        for rel in ("dist/app.1.js", "dist/keep.js"):
            with open(os.path.join(base, rel), "w", encoding="utf-8") as fh:
                fh.write(rel)

    def second(base):  # a cleaning build: old bundle and an input go
        os.remove(os.path.join(base, "dist/app.1.js"))
        os.remove(os.path.join(base, "notes.txt"))
        with open(os.path.join(base, "dist/app.2.js"), "w", encoding="utf-8") as fh:
            fh.write("v2")
        # A made file swapped for a link is not content, but it is not gone.
        os.remove(os.path.join(base, "dist/keep.js"))
        os.symlink("app.2.js", os.path.join(base, "dist/keep.js"))

    _stop_after(bridge_hook, [_RERUN])
    deletions = _cell_deletions(bridge_hook)
    res, _ = _run_oneshot(
        {"mode": "oneshot", "command": "npm run build",
         "serve_command": "npx serve dist", "serve_port": 3000},
        inputs={"notes.txt": "mine", "main.js": "src"},
        passes=[first, second], timeout=15,
    )
    assert res.ok, res.error
    assert [d for d in deletions if not d.startswith("_runs/")] == ["dist/app.1.js"]
    assert _flushed(res, "dist/app.2.js") == ["v2"]
    assert any("1 removed" in line for line in res.logs)


def test_oneshot_rerun_keeps_a_made_file_it_cannot_check__F1647(bridge_hook):
    """A made file the host cannot see any more is dropped only when it is
    known to be gone. Under a directory the host cannot enter (a
    `chmod 000 out`, or `chmod -R a-x` stripping the owner's x) it is still
    on disk as far as anyone knows: the cell keeps it, and the log says
    why."""
    if os.getuid() == 0:
        pytest.skip("root enters any directory: nothing to simulate")
    handbacks: list = []

    def first(base):
        os.makedirs(os.path.join(base, "out"), exist_ok=True)
        with open(os.path.join(base, "out", "a.js"), "w", encoding="utf-8") as fh:
            fh.write("a")

    def second(base):
        os.chmod(os.path.join(base, "out"), 0)

    def restore_at_teardown(base):
        handbacks.append(base)
        if len(handbacks) == 3:   # the teardown's, after both pull-backs
            os.chmod(os.path.join(base, "out"), 0o755)

    _stop_after(bridge_hook, [_RERUN])
    deletions = _cell_deletions(bridge_hook)
    res, _ = _run_oneshot(
        {"mode": "oneshot", "command": "make",
         "serve_command": "npx serve out", "serve_port": 3000},
        passes=[first, second], on_handback=restore_at_teardown, timeout=15,
    )
    assert res.ok, res.error
    assert len(handbacks) == 3
    assert [d for d in deletions if not d.startswith("_runs/")] == []
    assert _flushed(res, "out/a.js") == ["a"]
    assert any("kept 1 file(s)" in line and "out/a.js" in line for line in res.logs), res.logs


def test_oneshot_rerun_keeps_a_made_file_it_cannot_read__F1647(bridge_hook):
    """A made file still there as a regular file that the host cannot read
    (mode 000, or root's 0600 after a hand-back that failed) is not pulled
    back, and it is not gone either: the cell keeps it and nothing deletes
    it — not from the cell, and so not from the disk through
    gen.delete_file."""
    if os.getuid() == 0:
        pytest.skip("root reads any file: nothing to simulate")
    on_disk: list = []

    def first(base):
        os.makedirs(os.path.join(base, "out"), exist_ok=True)
        with open(os.path.join(base, "out", "a.js"), "w", encoding="utf-8") as fh:
            fh.write("a")

    def second(base):
        os.chmod(os.path.join(base, "out", "a.js"), 0)

    def at_teardown(base):
        on_disk.append(os.path.isfile(os.path.join(base, "out", "a.js")))
        if len(on_disk) == 3:   # the teardown's, after both pull-backs
            os.chmod(os.path.join(base, "out", "a.js"), 0o644)

    _stop_after(bridge_hook, [_RERUN])
    deletions = _cell_deletions(bridge_hook)
    res, _ = _run_oneshot(
        {"mode": "oneshot", "command": "make",
         "serve_command": "npx serve out", "serve_port": 3000},
        passes=[first, second], on_handback=at_teardown, timeout=15,
    )
    assert res.ok, res.error
    assert on_disk == [True, True, True]
    assert [d for d in deletions if not d.startswith("_runs/")] == []
    assert _flushed(res, "out/a.js") == ["a"]


def test_oneshot_rerun_drops_a_made_file_a_directory_replaced__F1647(bridge_hook):
    """A made file a later pass replaced with a directory is gone as a
    file: the cell drops it and takes the files inside the directory, so it
    never holds `dist/a` both as a file and as a directory."""
    def first(base):
        os.makedirs(os.path.join(base, "dist"), exist_ok=True)
        with open(os.path.join(base, "dist", "a"), "w", encoding="utf-8") as fh:
            fh.write("file")

    def second(base):
        os.remove(os.path.join(base, "dist", "a"))
        os.makedirs(os.path.join(base, "dist", "a"))
        with open(os.path.join(base, "dist", "a", "x"), "w", encoding="utf-8") as fh:
            fh.write("inside")

    _stop_after(bridge_hook, [_RERUN])
    deletions = _cell_deletions(bridge_hook)
    res, _ = _run_oneshot(
        {"mode": "oneshot", "command": "make",
         "serve_command": "npx serve dist", "serve_port": 3000},
        passes=[first, second], timeout=15,
    )
    assert res.ok, res.error
    assert [d for d in deletions if not d.startswith("_runs/")] == ["dist/a"]
    assert _flushed(res, "dist/a/x") == ["inside"]


def test_oneshot_run_txt_keeps_the_tail_of_huge_output__F1669():
    mod = _load_run_module()
    huge = "x" * (mod._RUN_TXT_TAIL_CHARS + 5000) + "THE END"
    res, _ = _run_oneshot({"mode": "oneshot", "command": "make"}, exec_output=huge)
    assert res.ok, res.error
    text = res.files["run.txt"]
    assert text.startswith("$ make\n(exit 0)\n\n[... ")
    assert text.endswith("THE END")
    assert len(text) < mod._RUN_TXT_TAIL_CHARS + 200
    # Output under the cap is kept whole, unmarked.
    assert mod._run_txt("ls", 0, "a\nb\n") == "$ ls\n(exit 0)\n\na\nb\n"
    # The host's own cut line stays above this one, so run.txt counts both.
    host = "[... 42000000 earlier bytes of output dropped]\n"
    text = mod._run_txt("make", 2, host + huge)
    head = text.split("\n")
    assert head[3] == host.rstrip("\n")
    assert head[4] == "[... 5007 more characters of output dropped]"
    assert text.endswith("THE END")


def test_disk_files_skips_a_fifo_without_blocking(tmp_path):
    import threading
    if not hasattr(os, "mkfifo"):
        return
    (tmp_path / "a.txt").write_text("a")
    os.mkfifo(tmp_path / "pipe")
    mod = _load_run_module()
    out = {}
    t = threading.Thread(target=lambda: out.update(mod._disk_files(str(tmp_path))),
                         daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():  # unblock the stuck reader so the process can exit
        os.close(os.open(tmp_path / "pipe", os.O_WRONLY | os.O_NONBLOCK))
    assert not t.is_alive(), "_disk_files blocked opening a FIFO"
    assert out == {"a.txt": b"a"}


def test_oneshot_scripts_run_before_command():
    res, rec = _run_oneshot(
        {"mode": "oneshot", "command": "pytest -q"},
        config_folders={"scripts": {"01_setup.sh": "pip install -r req.txt\n",
                                    "02_env.sh": "export X=1\n"}},
    )
    assert res.ok, res.error
    full = rec[-1]["execs"][0]
    # The scripts preamble (a sorted for-loop over the staged folder) precedes
    # the command in the single exec.
    assert "[scripts]" in full
    assert "folders/scripts" in full
    assert full.index("[scripts]") < full.index("pytest -q")


def test_oneshot_serve_shows_the_preview_until_stop(bridge_hook):
    """With serve_command + serve_port, oneshot backgrounds the server,
    exposes the port and stays alive until Stop. The preview pointer is a UI
    file in the run folder: committed, then revealed, and deleted at Stop —
    it points at a server that stops with the run."""
    _stop_after(bridge_hook)
    res, rec = _run_oneshot(
        {"mode": "oneshot", "command": "echo built",
         "serve_command": "python -m http.server 8000", "serve_port": 8000},
    )
    assert res.ok, res.error
    assert rec[-1]["bg"], "the serve command was never backgrounded"
    assert "http.server" in rec[-1]["bg"][0]
    preview = f"{_FOLDER}/preview.website"
    [pointer] = _flushed(res, preview)
    assert json.loads(pointer)["url"].endswith("/8000/")
    assert _reveals(res) == [(preview, True)]
    assert not any(p.startswith("_runs/") for p in res.files)
    assert "run.txt" in res.files       # work output stays at the cell root


def test_oneshot_serve_preview_rerun_reexecutes(bridge_hook):
    """The serve arm's preview carries a Re-run toolbar button: each press
    on it (its source is the preview's file) re-executes the command in the
    same container. A Re-run press from any other file runs nothing."""
    _stop_after(bridge_hook, [{"action_id": "rerun", "source": "notes.md"}, _RERUN, _RERUN])
    res, rec = _run_oneshot(
        {"mode": "oneshot", "command": "echo built",
         "serve_command": "python -m http.server 8000", "serve_port": 8000},
        timeout=15,
    )
    assert res.ok, res.error
    [pointer] = _flushed(res, _RERUN["source"])
    assert json.loads(pointer)["buttons"] == [{"id": "rerun", "label": "Re-run"}]
    # The command ran three times: the opening pass + one per preview press.
    assert len(rec[-1]["execs"]) == 3
    assert any("ran ×3" in line for line in res.logs)


def test_oneshot_leaves_the_run_folders_to_their_runs():
    """A command that touches files under `_runs/` (another run's surface,
    say) produces no work output there: the pull-back never stages a
    `_runs/` path, and says so in the log."""
    res, _ = _run_oneshot(
        {"mode": "oneshot", "command": "touch everything"},
        exec_writes={"made.txt": "work", "_runs/other-89abcdef/chat/x.chat": "{}"},
    )
    assert res.ok, res.error
    assert _flushed(res, "made.txt") == ["work"]
    assert not any(p.startswith("_runs/") for c in _RESULT_UPDATES[id(res)] for p in c["files"])
    assert any("_runs/other-89abcdef/chat/x.chat" in line for line in res.logs)


def test_terminal_serves_its_pointer_in_the_run_folder():
    """terminal mode: the `.terminal` pointer is the run's UI file
    `app.terminal`, revealed when written and deleted when the terminal
    ends."""
    res, rec = _run_oneshot({"mode": "terminal", "command": "lazygit"})
    assert res.ok, res.error
    pointer = f"{_FOLDER}/app.terminal"
    written = _flushed(res, pointer)   # the live mirror may re-send it unchanged
    assert written and {json.loads(w)["label"] for w in written} == {"lazygit"}
    assert _reveals(res) == [(pointer, True)]
    assert pointer not in res.files
    assert rec[-1]["closed"]












def test_app_sync_skip_dirs_keep_the_live_files_defaults():
    # A skip_dirs argument replaces gen.live_files()'s default set, so the
    # app's set must re-list every default (a workspace .venv is not content).
    import collimate.fs

    mod = _load_run_module()
    assert mod._APP_SYNC_SKIP_DIRS >= collimate.fs.WORKSPACE_SKIP_DIRS
    assert ".vscode" in mod._APP_SYNC_SKIP_DIRS


def test_app_readiness_counts_an_http_error_status_as_up(monkeypatch):
    # A server whose root answers 404 (an API-only app) is listening: the
    # readiness wait must not run out its timeout on it.
    import asyncio
    import http.server
    import threading

    class _NotFound(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_error(404)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _NotFound)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        mod = _load_run_module()
        monkeypatch.setattr(mod, "gen", types.SimpleNamespace(cancelled=False))
        assert asyncio.run(mod._wait_for_ready(srv.server_address[1], timeout=3))
    finally:
        srv.shutdown()
        srv.server_close()


def test_app_serves_the_host_port_the_container_port_is_published_on():
    # The host proxies only a port the run's environment published, so app
    # mode serves the host side of the mapping even when the two differ.
    records: list = []
    mod = _load_run_module()
    mod.DockerEnvironment = _make_fake(records)

    async def _ready(port, timeout=120):
        return port == 18080

    mod._wait_for_ready = _ready
    res = run_generator_callable(
        mod.run, config={"settings.set_yaml": {"mode": "app", "expose_port": 8080}},
        timeout=10, label=_LABEL, ui_files=_UI_FILES,
    )
    assert res.ok, res.error
    assert records[-1]["ports"] == {"8080/tcp": ("127.0.0.1", None)}
    served = [call for call in res.tier_b_calls if call["op"] == "serve_port"]
    assert served == [{"op": "serve_port", "port": 18080, "name": "App", "path": ""}]
    # The preview pointer: a UI file, revealed, gone once the app stops.
    pointer = f"{_FOLDER}/app.website"
    written = _flushed(res, pointer)   # the live mirror may re-send it unchanged
    assert written and all("18080" in w for w in written)
    assert _reveals(res) == [(pointer, True)]
    assert pointer not in res.files


def test_app_placeholder_ignores_the_run_folders():
    """An app over a cell whose only files are runs' surfaces still gets the
    README placeholder: those files are not something to edit in the app."""
    records: list = []
    mod = _load_run_module()
    mod.DockerEnvironment = _make_fake(records)

    async def _ready(port, timeout=120):
        return True

    mod._wait_for_ready = _ready
    res = run_generator_callable(
        mod.run, config={"settings.set_yaml": {"mode": "app"}},
        timeout=10, label=_LABEL, ui_files=_UI_FILES,
        foreign_runs={"other-89abcdef": {"chat/conversation.chat": "{}"}},
    )
    assert res.ok, res.error
    assert "Empty workspace" in res.files.get("README.md", "")


_GIT = {"git": git_account_snapshot("github", "api_key", secrets={"api_key": "ghp_s3cret"})}
_AUTHOR = ("Ada Lovelace", "ada@example.org")


def _bound(snapshot, monkeypatch):
    """The GitAccount ``snapshot`` resolves to, outside a run."""
    bind_account(monkeypatch, snapshot)
    return git_account.git_account()


def test_app_gets_git_credentials_by_exec_after_ready_never_in_its_env__F1657_F1956():
    """App mode keeps the Git token out of the container's environment
    (inherited by code-server, every terminal, extension and agent in it,
    and shown by `docker inspect`): once the app answers, the editor's user
    gets it through one exec's environment, as terminal mode does. The
    startup commands the entrypoint launches at once wait for it: the
    generator flags the wait and touches the marker they wait on once the
    credentials are written. The commit author is the account's."""
    records: list = []
    mod = _load_run_module()
    mod.DockerEnvironment = _make_fake(records)

    async def _ready(port, timeout=120):
        records[-1]["container_calls"].append("ready")
        return True

    mod._wait_for_ready = _ready
    res = run_generator_callable(
        mod.run, config={"settings.set_yaml": {
            "mode": "app", "startup_commands": "git clone https://github.com/octo/private"}},
        timeout=10, label=_LABEL, ui_files=_UI_FILES, accounts=_GIT,
    )
    assert res.ok, res.error
    env = records[-1]["environment"]
    assert not any(k.startswith("GITHUB") for k in env), env
    assert "ghp_s3cret" not in json.dumps(env)
    assert (env["GIT_USER_NAME"], env["GIT_USER_EMAIL"]) == _AUTHOR
    assert env["COLLIMATE_AWAIT_GIT_LOGIN"] == "1"
    calls = records[-1]["container_calls"]
    [cred] = [c for c in calls if c != "ready"
              and "COLLIMATE_GIT_CRED" in (c.get("environment") or {})]
    assert "ghp_s3cret" in cred["environment"]["COLLIMATE_GIT_CRED"]
    assert "ghp_s3cret" not in json.dumps(cred["cmd"])
    assert cred["user"] == "coder"
    assert calls.index("ready") < calls.index(cred)
    [release] = [c for c in calls if c != "ready" and mod._GIT_LOGIN_READY in json.dumps(c["cmd"])]
    assert calls.index(cred) < calls.index(release) and release["user"] == "coder"
    assert not any("ghp_s3cret" in line for line in res.logs)
    # The entrypoint waits on the same marker, only when flagged.
    for tree in ("launch/configs/vscode",):
        path = os.path.join(_GEN_DIR, "launch", tree, "files", "build", "entrypoint.sh")
        text = open(path, encoding="utf-8").read()
        assert "COLLIMATE_AWAIT_GIT_LOGIN" in text and mod._GIT_LOGIN_READY in text, path


def _app_with_login(records, *, startup_commands="", container=None):
    """Run app mode with the Git account bound and ``startup_commands``;
    ``container`` replaces the fake's container class."""
    mod = _load_run_module()
    fake = _make_fake(records)
    if container is not None:
        class _Fake(fake):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.container = container(self._rec["container_calls"])
        fake = _Fake
    mod.DockerEnvironment = fake

    async def _ready(port, timeout=120):
        return True

    mod._wait_for_ready = _ready
    cfg = {"mode": "app", **({"startup_commands": startup_commands} if startup_commands else {})}
    res = run_generator_callable(
        mod.run, config={"settings.set_yaml": cfg},
        timeout=10, label=_LABEL, ui_files=_UI_FILES, accounts=_GIT,
    )
    return mod, res


def test_app_releases_the_startup_commands_when_the_login_raises__F1657():
    """The startup commands wait for the Git account; a credential write
    that raises (the daemon dropped the exec) is logged, the app still
    serves, and the commands are released all the same, by the editor's
    user."""
    records: list = []

    class _Dropping(_FakeContainer):
        def exec_run(self, cmd, **k):
            super().exec_run(cmd, **k)
            if "COLLIMATE_GIT_CRED" in (k.get("environment") or {}):
                raise RuntimeError("the daemon dropped the exec")
            return types.SimpleNamespace(exit_code=0, output=b"OK")

    mod, res = _app_with_login(records, startup_commands="git clone https://github.com/o/p",
                               container=_Dropping)
    assert res.ok, res.error
    assert any("Git credentials were not configured in the app: the daemon dropped the exec"
               in line for line in res.logs), res.logs
    calls = records[-1]["container_calls"]
    [cred] = [c for c in calls if "COLLIMATE_GIT_CRED" in (c.get("environment") or {})]
    [release] = [c for c in calls if mod._GIT_LOGIN_READY in json.dumps(c["cmd"])]
    assert calls.index(cred) < calls.index(release) and release["user"] == "coder"
    assert records[-1]["environment"]["COLLIMATE_AWAIT_GIT_LOGIN"] == "1"
    assert not any("ghp_s3cret" in line for line in res.logs)


def test_app_without_startup_commands_holds_nothing_back__F1657():
    """With an account but no startup commands, nothing waits for it: no
    wait flag reaches the entrypoint, and there is nothing to release."""
    records: list = []
    mod, res = _app_with_login(records)
    assert res.ok, res.error
    assert "COLLIMATE_AWAIT_GIT_LOGIN" not in records[-1]["environment"]
    calls = records[-1]["container_calls"]
    assert any("COLLIMATE_GIT_CRED" in (c.get("environment") or {}) for c in calls)
    assert not any(mod._GIT_LOGIN_READY in json.dumps(c["cmd"]) for c in calls)


def test_a_failed_credential_write_is_logged_not_claimed__F1657(monkeypatch):
    """An image without bash or without the user exits non-zero instead of
    raising: that is logged as a failure, with every form of the token
    kept out of it."""
    logs: list = []
    mod = _load_run_module()
    account = _bound(_GIT["git"], monkeypatch)
    monkeypatch.setattr(mod, "gen", types.SimpleNamespace(log=logs.append))
    leak = " ".join(account.secret_forms())

    class _NoBashEnvironment:
        def exec(self, command, **kwargs):
            if kwargs.get("env"):
                return 127, f"bash: not found; {leak} leaked\n"
            return 0, "OK"

    mod._configure_git(_NoBashEnvironment(), account=account, user="coder")
    assert not any("Configured the Git account" in line for line in logs), logs
    assert any("were not configured (exit 127)" in line for line in logs), logs
    assert not any(form in line for line in logs for form in account.secret_forms())


def test_oneshot_gets_git_credentials_by_one_root_exec__F1657():
    """Oneshot's command runs as root, so the credential is written for
    root, through one exec's environment: never on an argv, in the
    container's environment or in the log."""
    res, rec = _run_oneshot({"mode": "oneshot", "command": "git pull"}, accounts=_GIT)
    assert res.ok, res.error
    [cred] = [c for c in rec[-1]["container_calls"]
              if "COLLIMATE_GIT_CRED" in (c.get("environment") or {})]
    assert "ghp_s3cret" in cred["environment"]["COLLIMATE_GIT_CRED"]
    assert cred["user"] == "root"
    assert "ghp_s3cret" not in json.dumps(cred["cmd"])
    assert "ghp_s3cret" not in json.dumps(rec[-1]["environment"])
    assert not any("ghp_s3cret" in line for line in res.logs)


def _git_account(service="github", auth="api_key", **changes):
    return {"git": git_account_snapshot(service, auth, **changes)}


def _run_mode(mode, *, accounts=None, settings=None, permissions=None, records=None, patch=None,
              container=None):
    """Run ``mode`` (oneshot, terminal or app) with Docker faked and the app
    ready at once; return (result, records). ``patch(module)`` adjusts the
    loaded generator module first; ``container`` replaces the fake's
    container class."""
    records = records if records is not None else []
    mod = _load_run_module()
    fake = _make_fake(records)
    if container is not None:
        class _Fake(fake):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.container = container(self._rec["container_calls"])
        fake = _Fake
    mod.DockerEnvironment = fake
    if patch:
        patch(mod)

    async def _ready(port, timeout=120):
        return True

    mod._wait_for_ready = _ready
    cfg = {"mode": mode, **({"command": "git pull"} if mode == "oneshot" else {}), **(settings or {})}
    res = run_generator_callable(
        mod.run, config={"settings.set_yaml": cfg}, accounts=accounts,
        permissions=permissions, timeout=10, label=_LABEL, ui_files=_UI_FILES,
    )
    return res, records


def _credential_calls(records, name="COLLIMATE_GIT_CRED"):
    return [c for c in records[-1]["container_calls"]
            if name in (c.get("environment") or {})]


def _git_config_calls(records):
    return [c for c in records[-1]["container_calls"] if "git config" in " ".join(map(str, c["cmd"]))]


@pytest.mark.parametrize("mode", ["oneshot", "terminal", "app"])
@pytest.mark.parametrize("account, message", [
    (_git_account(secrets={}), "has no token; add it in Key manager"),
    (_git_account(endpoint="https://api.github.com"), "address cannot be used"),
    (_git_account(kind="account"), "needs a GitHub, GitLab or Git server entry from Key manager"),
    (_git_account(metadata={"author_email": None}), "has no valid commit author name and email"),
])
def test_broken_git_account_fails_cleanly_in_every_mode(mode, account, message):
    """A bound Git account that cannot serve ends the run through error.md
    in every mode, before any container is placed."""
    res, rec = _run_mode(mode, accounts=account)
    assert res.ok, res.error
    assert message in res.files.get("error.md", "")
    assert not rec


@pytest.mark.parametrize("mode", ["oneshot", "terminal", "app"])
def test_bound_account_writes_the_credential_through_one_exec_env(mode):
    """The account's token reaches the container's git as one credential
    line behind a store helper scoped to the account's base, through one
    exec's environment: written under umask 077 from $COLLIMATE_GIT_CRED,
    never on an argv, in the container's environment or in the log.
    Oneshot writes it for root, the terminal and the app for the editor's
    user."""
    res, rec = _run_mode(mode, accounts=_GIT)
    assert res.ok, res.error
    [cred] = _credential_calls(rec)
    assert cred["environment"] == {"COLLIMATE_GIT_CRED": "https://x-access-token:ghp_s3cret@github.com",
                                   "COLLIMATE_GIT_WEB": "https://github.com/"}
    cmd = json.dumps(cred["cmd"])
    assert "$COLLIMATE_GIT_CRED" in cmd and "umask 077" in cmd and "ghp_s3cret" not in cmd
    assert cred["user"] == ("root" if mode == "oneshot" else "coder")
    calls = rec[-1]["container_calls"]
    assert not any("ghp_s3cret" in json.dumps(c["cmd"]) for c in calls)
    assert "ghp_s3cret" not in json.dumps(rec[-1].get("environment") or {})
    assert not any("ghp_s3cret" in line for line in res.logs)
    assert any("Configured the Git account's token for HTTPS git under https://github.com" in line
               for line in res.logs), res.logs


@pytest.mark.parametrize("snapshot, line, web", [
    (git_account_snapshot("github", "api_key"), "https://x-access-token:ghp_fake@github.com",
     "https://github.com/"),
    (git_account_snapshot("github", "api_key", endpoint="https://ghe.example.com"),
     "https://x-access-token:ghp_fake@ghe.example.com", "https://ghe.example.com/"),
    (git_account_snapshot("gitlab", "api_key", endpoint="https://example.com/gitlab"),
     "https://oauth2:glpat-fake@example.com", "https://example.com/gitlab/"),
    (git_account_snapshot("git", "basic", metadata={"username": "ada lo"}),
     "https://ada%20lo:pw-fake@git.example.org", "https://git.example.org/"),
])
def test_credential_line_per_service(snapshot, line, web):
    """One credential line per account: the service's Basic user (the
    account's own for a Git server), the host of its base and no path; the
    exec sets only the store helper scoped to that base, never a global
    credential.helper."""
    res, rec = _run_mode("terminal", accounts={"git": snapshot})
    assert res.ok, res.error
    [cred] = _credential_calls(rec)
    assert cred["environment"] == {"COLLIMATE_GIT_CRED": line, "COLLIMATE_GIT_WEB": web}
    script = cred["cmd"][-1]
    assert 'git config --global "credential.$COLLIMATE_GIT_WEB.helper" store' in script
    assert "credential.helper" not in script
    assert f"under {web.rstrip('/')}" in " ".join(res.logs)


def test_credential_is_scoped_to_the_account_base_path(tmp_path):
    """Real git, no container: the exec's own script, run in a temporary
    HOME for an account under https://example.com/gitlab, makes git offer
    the token for a repository under that base and for no other path on
    the same host."""
    import shutil
    import subprocess
    git = shutil.which("git")
    bash = shutil.which("bash")
    if not git or not bash:
        pytest.skip("git and bash are needed")
    mod = _load_run_module()
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    done = subprocess.run([bash, "-c", mod._TOKEN_SCRIPT], env={
        **env, "COLLIMATE_GIT_CRED": "https://oauth2:glpat-fake@example.com",
        "COLLIMATE_GIT_WEB": "https://example.com/gitlab/"}, capture_output=True)
    assert done.returncode == 0
    assert oct((home / ".git-credentials").stat().st_mode & 0o777) == "0o600"

    def fill(path):
        out = subprocess.run([git, "credential", "fill"], env=env, capture_output=True, text=True,
                             input=f"protocol=https\nhost=example.com\npath={path}\n\n")
        return out.returncode, "password=glpat-fake" in out.stdout

    assert fill("gitlab/g/p.git") == (0, True)
    code, offered = fill("other/x.git")
    assert code != 0 and not offered


def test_oneshot_never_forwards_the_ssh_agent(tmp_path, monkeypatch):
    """git in the container authenticates only with the run's Git account:
    the host's SSH agent socket is neither mounted nor named, and the
    manifest passes no SSH_AUTH_SOCK through."""
    import yaml
    sock = tmp_path / "agent.sock"
    sock.write_text("")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))
    res, rec = _run_oneshot({"mode": "oneshot", "command": "git clone git@x:y ."})
    assert res.ok, res.error
    assert "SSH_AUTH_SOCK" not in json.dumps(rec[-1]["volumes"]) and str(sock) not in json.dumps(rec[-1]["volumes"])
    assert "SSH_AUTH_SOCK" not in rec[-1]["environment"]
    with open(os.path.join(_GEN_DIR, "launch", "launch", "launch.generator"), encoding="utf-8") as fh:
        assert "SSH_AUTH_SOCK" not in json.dumps(yaml.safe_load(fh))


def test_unbound_oneshot_runs_no_git_exec():
    res, rec = _run_mode("oneshot")
    assert res.ok, res.error
    assert rec and _git_config_calls(rec) == []
    assert not any("Git account" in line for line in res.logs), res.logs


@pytest.mark.parametrize("mode", ["terminal", "app"])
def test_unbound_sets_no_identity_and_logs(mode):
    """Terminal and app modes set git's defaults without an account, but no
    commit author and no credential, and say how to commit."""
    res, rec = _run_mode(mode)
    assert res.ok, res.error
    calls = _git_config_calls(rec)
    assert calls and not any("user.name" in json.dumps(c["cmd"]) or "user.email" in json.dumps(c["cmd"])
                             for c in calls)
    assert _credential_calls(rec) == []
    assert "GIT_USER_NAME" not in (rec[-1].get("environment") or {})
    assert any("No Git account bound: commits in the container need git config user.name and user.email"
               in line for line in res.logs), res.logs


@pytest.mark.parametrize("mode", ["oneshot", "terminal", "app"])
@pytest.mark.parametrize("snapshot", [
    git_account_snapshot("gitlab", "api_key"), git_account_snapshot("git", "none"),
])
def test_identity_comes_from_the_account_in_every_mode(mode, snapshot):
    res, rec = _run_mode(mode, accounts={"git": snapshot})
    assert res.ok, res.error
    user = "root" if mode == "oneshot" else "coder"
    calls = rec[-1]["container_calls"]
    assert {"cmd": ["git", "config", "--global", "user.name", _AUTHOR[0]], "user": user,
            "environment": None} in calls
    assert {"cmd": ["git", "config", "--global", "user.email", _AUTHOR[1]], "user": user,
            "environment": None} in calls
    if mode == "app":
        env = rec[-1]["environment"]
        assert (env["GIT_USER_NAME"], env["GIT_USER_EMAIL"]) == _AUTHOR
    if snapshot["auth_type"] == "none":
        assert _credential_calls(rec) == [] and _credential_calls(rec, "COLLIMATE_GIT_KEY") == []


@pytest.mark.parametrize("mode", ["oneshot", "terminal", "app"])
def test_ssh_account_installs_key_and_known_hosts_through_one_exec_env(mode):
    """An SSH-key account's key and known hosts reach ~/.ssh through ONE
    exec's environment (never an argv), with core.sshCommand pinning them;
    no URL is rewritten, so other HTTPS remotes stay HTTPS."""
    from collimate import git_hosts
    snapshot = git_account_snapshot("github", "ssh_key", metadata={
        "known_hosts": "box.example.org " + git_hosts.PINNED_HOST_KEYS["github.com"][0].split(" ", 1)[1]})
    res, rec = _run_mode(mode, accounts={"git": snapshot})
    assert res.ok, res.error
    key = snapshot["secrets"]["private_key"]
    [install] = _credential_calls(rec, "COLLIMATE_GIT_KEY")
    assert install["environment"]["COLLIMATE_GIT_KEY"] == key
    known = install["environment"]["COLLIMATE_GIT_KNOWN_HOSTS"].splitlines()
    assert [line.split()[0] for line in known].count("github.com") == 3 and known[-1].startswith("box.example.org ")
    script = install["cmd"][-1]
    assert "command -v ssh >/dev/null 2>&1 || exit 3" in script
    assert "core.sshCommand" in script and "StrictHostKeyChecking=yes" in script
    assert "insteadOf" not in script and "pushInsteadOf" not in script
    assert install["user"] == ("root" if mode == "oneshot" else "coder")
    body = [line for line in key.splitlines() if not line.startswith("-----")]
    assert not any(part in json.dumps(c["cmd"]) for c in rec[-1]["container_calls"] for part in body)
    assert _credential_calls(rec) == []
    assert any("Configured the Git account's SSH key in the container" in line for line in res.logs)


def test_image_without_ssh_logs_and_continues():
    class _NoSsh(_FakeContainer):
        def exec_run(self, cmd, **k):
            super().exec_run(cmd, **k)
            if "COLLIMATE_GIT_KEY" in (k.get("environment") or {}):
                return types.SimpleNamespace(exit_code=3, output=b"")
            return types.SimpleNamespace(exit_code=0, output=b"OK")

    res, rec = _run_mode("terminal", accounts=_git_account("gitlab", "ssh_key"), container=_NoSsh)
    assert res.ok, res.error
    assert "error.md" not in res.files
    assert any("This image has no ssh client, so the Git account's SSH key was not installed" in line
               for line in res.logs), res.logs
    assert not any("Configured the Git account's SSH key" in line for line in res.logs)


@pytest.mark.parametrize("mode", ["oneshot", "terminal", "app"])
@pytest.mark.parametrize("snapshot", [
    git_account_snapshot("gitlab", "api_key", endpoint="https://example.com/gitlab"),
    git_account_snapshot("git", "basic"),
    git_account_snapshot("gitlab", "ssh_key"),
])
def test_no_git_secret_in_container_env_or_logs(mode, snapshot, monkeypatch):
    """No form of the account's secret reaches the container's environment,
    any argv, the run's log or its files; a failing install's output is
    scrubbed of every form."""
    with monkeypatch.context() as patched:
        forms = _bound(snapshot, patched).secret_forms()

    class _Failing(_FakeContainer):
        def exec_run(self, cmd, **k):
            super().exec_run(cmd, **k)
            if {"COLLIMATE_GIT_CRED", "COLLIMATE_GIT_KEY"} & set(k.get("environment") or {}):
                return types.SimpleNamespace(exit_code=1, output=f"failed: {' '.join(forms)}".encode())
            return types.SimpleNamespace(exit_code=0, output=b"OK")

    res, rec = _run_mode(mode, accounts={"git": snapshot}, container=_Failing)
    assert res.ok, res.error
    seen = json.dumps([rec[-1].get("environment") or {}, rec[-1].get("volumes") or {},
                       [c["cmd"] for c in rec[-1]["container_calls"]], res.logs, res.files])
    leaked = [form for form in forms if form in seen]
    assert leaked == [], len(leaked)
    assert any("were not configured (exit 1)" in line for line in res.logs), res.logs


def test_release_lookups_get_only_a_github_com_token(monkeypatch, tmp_path):
    """Release lookups (`github:` sources) ride the Git account's token only
    when it is a github.com token; any other account, or a broken one,
    makes them anonymous with a log line instead of failing the run, and
    with update_packages off the account is not consulted at all."""
    import _env_shared.upstream as upstream
    mod = _load_run_module()
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("ARG CODE_SERVER_VERSION=4.140.0\nFROM x\n")
    monkeypatch.setattr(upstream, "_read_record", lambda: {})
    monkeypatch.setattr(upstream, "_write_record", lambda record: None)
    seen: list = []

    def latest(source, github_token=None):
        seen.append(github_token)
        return "4.141.0"

    monkeypatch.setattr(upstream, "latest_version", latest)

    def versions(accounts, permissions=None):
        async def run():
            gen.update_file("versions.json", json.dumps(mod._requested_versions(str(dockerfile))))
        return run_generator_callable(run, accounts=accounts, permissions=permissions, timeout=10)

    res = versions(_GIT)
    assert res.ok, res.error
    assert seen == ["ghp_s3cret"]
    for accounts, why in [(_git_account(secrets={}), "has no token"),
                          (_git_account("gitlab", "api_key"), "the Git account is not a github.com token"),
                          (_git_account(endpoint="https://ghe.example.com"), "not a github.com token"),
                          (_git_account("github", "ssh_key"), "not a github.com token")]:
        seen.clear()
        res = versions(accounts)
        assert res.ok, res.error
        assert seen == [None]
        assert any("[upstream] GitHub release lookups are anonymous" in line and why in line
                   for line in res.logs), res.logs
    seen.clear()

    def consulted(log=None):
        raise AssertionError("release_token consulted without update_packages")

    monkeypatch.setattr(git_account, "release_token", consulted)
    res = versions(_GIT, {"docker": "full", "internet": "full", "update_packages": False})
    assert res.ok, res.error
    assert seen == []


def test_app_hands_the_workspace_back_when_it_stops__F1966_F1968(monkeypatch):
    """The editor's user (uid 1000 in the VS Code image, or root through its
    passwordless sudo) writes as its own uid: stopping the app freezes
    every process in the container and hands /workspace back to its owner
    before the live mirror's last flush and before the container goes.
    The owner is the host's view of the workspace root, read before the
    container is placed — its entrypoint launches the startup commands
    at once, and a `sudo chown coder /workspace` there would otherwise
    decide who gets the workspace back. A probe at stop would read the
    other id maps here and target another owner."""
    records: list = []
    mod = _load_run_module()
    mod.DockerEnvironment = _make_fake(records, owner_out=[_IDENTITY, _REMAPPED])
    seen, patch = _owner_seen_at(records, (4242, 4343))
    patch(mod)

    async def _ready(port, timeout=120):
        records[-1]["timeline"].append("ready")
        return True

    real_live_files = gen.live_files

    @contextlib.asynccontextmanager
    async def _live_files(**kw):
        async with real_live_files(**kw):
            yield
        records[-1]["timeline"].append("mirror flushed")

    monkeypatch.setattr(gen, "live_files", _live_files)

    mod._wait_for_ready = _ready
    res = run_generator_callable(
        mod.run, config={"settings.set_yaml": {"mode": "app"}},
        timeout=10, label=_LABEL, ui_files=_UI_FILES,
    )
    assert res.ok, res.error
    assert seen == [0], "the host owner was read after the container was placed"
    # The mirror's last flush comes after the hand-back, so it carries what
    # the hand-back makes the host user's (a root 0600 file a sudo'd
    # command wrote).
    assert records[-1]["timeline"] == ["owner", "ready", "freeze", "handback",
                                       "mirror flushed", "close"]
    [probe] = records[-1]["probes"]
    assert "/proc/self/uid_map" in probe[2]
    [script] = records[-1]["handbacks"]
    assert "/workspace" in script[-1] and "chown -h 4242:4343" in script[-1]


def test_app_that_never_answers_still_hands_back__F1966():
    """An app that stops before its mirror ever ran (it never answered,
    Stop came during the wait, or something failed first) still had its
    startup commands (an npm install, a git clone) writing /workspace
    since the container started: the teardown freezes them and hands the
    workspace back to the host's owner before the container goes."""
    records: list = []
    mod = _load_run_module()
    mod.DockerEnvironment = _make_fake(records)
    seen, patch = _owner_seen_at(records, (4242, 4343))
    patch(mod)

    async def _never(port, timeout=120):
        records[-1]["timeline"].append("not ready")
        return False

    mod._wait_for_ready = _never
    res = run_generator_callable(
        mod.run, config={"settings.set_yaml": {"mode": "app"}},
        timeout=10, label=_LABEL, ui_files=_UI_FILES,
    )
    assert not res.ok and "did not become ready" in (res.error or ""), res.error
    assert seen == [0]
    assert records[-1]["timeline"] == ["owner", "not ready", "freeze", "handback", "close"]
    [script] = records[-1]["handbacks"]
    assert "/workspace" in script[-1] and "chown -h 4242:4343" in script[-1]


def test_vscode_image_takes_no_credentials_from_its_environment__F1956():
    """The entrypoint seeds no git credentials from the environment and the
    generator passes none: the token reaches the editor only through
    ~/.git-credentials, written by one exec."""
    for tree in ("launch/configs/vscode",):
        path = os.path.join(_GEN_DIR, "launch", tree, "files", "build", "entrypoint.sh")
        text = open(path, encoding="utf-8").read()
        assert "GITHUB_" not in text and "git-credentials" not in text, path


def test_launch_declares_its_ui_files():
    import yaml

    with open(os.path.join(_GEN_DIR, "launch", "launch", "launch.generator"),
              encoding="utf-8") as fh:
        man = yaml.safe_load(fh)
    assert man["ui_files"] == [".terminal", ".website"]


def test_oneshot_exposes_prompt_to_command_env():
    # The run's prompt reaches the command as $COLLIMATE_PROMPT (git-clone runner).
    res, rec = _run_oneshot(
        {"mode": "oneshot", "command": 'git clone "$COLLIMATE_PROMPT" .'},
        prompt="https://example.com/repo.git",
    )
    assert res.ok, res.error
    assert rec[-1]["environment"].get("COLLIMATE_PROMPT") == "https://example.com/repo.git"


def test_oneshot_requires_a_command():
    res, rec = _run_oneshot({"mode": "oneshot", "command": ""})
    assert res.ok, res.error
    assert not rec, "no container should be started without a command"
    assert "no command" in res.files.get("error.md", "").lower()


# ---------------------------------------------------------------------------
# Mode dispatch — each mode reaches the container and degrades cleanly when
# Docker is unreachable. Simulate absence explicitly (a dead DOCKER_HOST)
# rather than relying on the docker package being uninstalled, so the tests
# hold on machines where Docker IS available.
# ---------------------------------------------------------------------------


def _degrade(config, bridge_hook):
    # Placement is host-mediated now: the mediation-era stand-in for "docker
    # daemon unreachable" is the host refusing env_place.
    bridge_hook["setup"] = lambda b: b.stub_env_place(
        lambda _spec: {
            "ok": False, "env_id": "", "container_id": "",
            "error": "docker daemon unreachable",
        }
    )
    res = run_generator(
        _RUN_PY, config={"settings.set_yaml": config},
        inputs={"main.py": "print(1)"}, timeout=20,
    )
    return res


def test_oneshot_degrades_without_docker(bridge_hook):
    res = _degrade({"mode": "oneshot", "command": "python main.py"}, bridge_hook)
    assert res.ok, res.error
    assert "could not start the container environment" in res.files.get("error.md", "")


def test_terminal_degrades_without_docker(bridge_hook):
    res = _degrade({"mode": "terminal", "command": "bash"}, bridge_hook)
    assert res.ok, res.error
    assert "could not start the terminal environment" in res.files.get("error.md", "")




def test_app_start_failure_finishes_failed_with_reason__P0F07(bridge_hook):
    res = _degrade({"mode": "app", "expose_port": 8080}, bridge_hook)
    assert not res.ok
    assert "could not start the app" in res.error
    assert "could not start the app" in res.files.get("error.md", "")


# ---------------------------------------------------------------------------
# Every template runner manifest lints clean and resolves against `run`
# ---------------------------------------------------------------------------


def test_run_tree_lints_clean():
    issues = lint_path(os.path.join(_GEN_DIR, "launch"))
    errors = [i for i in issues if i.level is Level.ERROR]
    warnings = [i for i in issues if i.level is Level.WARNING]
    assert not errors, "\n".join(i.format() for i in errors)
    # No warnings either — the launch generator + configs are the clean 1.0 surface.
    assert not warnings, "\n".join(i.format() for i in warnings)




def test_expected_configs_present__F1879():
    runners_dir = os.path.join(_GEN_DIR, "launch", "launch", "configs")
    present = {
        name for name in os.listdir(runners_dir)
        if os.path.isfile(os.path.join(runners_dir, name, "manifest.config_yaml"))
    }
    assert present == {"terminal", "lazygit", "code-runner", "default",
                       "vscode", "git-clone"}


def test_terminal_and_lazygit_share_batteries_image():
    # Identical build folders → identical content → the launch generator builds the
    # image once and both runners reuse it.
    a = os.path.join(_GEN_DIR, "launch", "launch", "configs", "terminal", "files", "build", "Dockerfile")
    b = os.path.join(_GEN_DIR, "launch", "launch", "configs", "lazygit", "files", "build", "Dockerfile")
    assert open(a, "rb").read() == open(b, "rb").read()


def test_vscode_runner_ships_build_assets():
    build = os.path.join(_GEN_DIR, "launch", "launch", "configs", "vscode", "files", "build")
    assert os.path.isfile(os.path.join(build, "Dockerfile"))
    assert os.path.isfile(os.path.join(build, "entrypoint.sh"))
    assert os.path.isfile(os.path.join(build, "bootstrap-ext", "package.json"))


def test_launch_floats_the_tools_its_dockerfile_declares(monkeypatch, tmp_path):
    """A Docker harness keeps itself current: a tool the runner's Dockerfile
    carries a version ARG for resolves to "latest" unless the runner pins it,
    and a tool the image does not install is never requested (it would
    re-tag the image on every unrelated release)."""
    import gen
    import _env_shared.upstream as upstream
    mod = _load_run_module()
    seen: dict = {}

    def _resolve(artifacts, requests, *, allow_network, log=None, github_token=None):
        seen.update(requests=dict(requests), allow_network=allow_network)
        return {}

    monkeypatch.setattr(upstream, "resolve_requests", _resolve)
    monkeypatch.setattr(git_account, "release_token", lambda log=None: None)
    from collimate.permissions import UNRESTRICTED
    monkeypatch.setattr(gen, "grant", lambda: UNRESTRICTED)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM x\nARG UV_VERSION=0.12.0\n  ARG CLAUDE_CODE_VERSION=2.1.220\nRUN true\n")
    monkeypatch.setattr(gen, "packages", lambda: {})
    mod._requested_versions(str(dockerfile))
    assert seen["requests"] == {"claude-code": "latest", "uv": "latest"}
    monkeypatch.setattr(gen, "packages", lambda: {"uv": "0.11.0"})
    mod._requested_versions(str(dockerfile))
    assert seen["requests"] == {"claude-code": "latest", "uv": "0.11.0"}
    # A Dockerfile that declares none of them: nothing to resolve.
    plain = tmp_path / "Plain"
    plain.write_text("FROM x\nRUN true\n")
    monkeypatch.setattr(gen, "packages", lambda: {})
    seen.clear()
    assert mod._requested_versions(str(plain)) == {} and seen == {}


@pytest.mark.parametrize('exit_code', [0, 17])
def test_terminal_program_exit_ends_run_and_releases_environment__F1643_F2031(bridge_hook, exit_code):
    def setup(bridge):
        bridge._autocancel_on_serve = False
        bridge.terminal_status = lambda terminal_id: {'exists': True, 'ended': True, 'exit_code': exit_code}
    bridge_hook['setup'] = setup
    result, records = _run_oneshot({'mode': 'terminal'}, timeout=0.4)
    assert result.ok == (exit_code == 0), result.error
    assert any(f'exit code {exit_code}' in line for line in result.logs), result.logs
    assert records[-1]['closed']
    assert not any(path.endswith('.terminal') for path in result.files)


def test_app_lets_docker_assign_port_before_readiness_and_serve__F1633():
    records = []
    def patch(mod):
        original = mod.DockerEnvironment
        class Environment(original):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.container.attrs = {'NetworkSettings': {'Ports': {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '38123'}]}}}
        mod.DockerEnvironment = Environment
        async def ready(port, timeout=120):
            assert records[-1]['ports'] == {'8080/tcp': ('127.0.0.1', None)}
            assert port == 38123
            return True
        mod._wait_for_ready = ready
    result, _ = _run_oneshot({'mode': 'app', 'mount_mode': 'ro'}, records=records, patch=patch)
    assert result.ok, result.error
    assert any(call['op'] == 'serve_port' and call['port'] == 38123 for call in result.tier_b_calls)


@pytest.mark.parametrize('bindings', [None, [], [{'HostIp': '0.0.0.0', 'HostPort': '38123'}], [{'HostIp': '127.0.0.1', 'HostPort': '0'}]])
def test_app_refuses_missing_or_non_loopback_docker_binding_and_closes__F1633(bindings):
    records = []
    def patch(mod):
        original = mod.DockerEnvironment
        class Environment(original):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.container.attrs = {'NetworkSettings': {'Ports': {'8080/tcp': bindings}}}
        mod.DockerEnvironment = Environment
    result, _ = _run_oneshot({'mode': 'app', 'mount_mode': 'ro'}, records=records, patch=patch)
    assert not result.ok and 'Docker' in result.error
    assert records[-1]['closed']
    assert not any(call['op'] == 'serve_port' for call in result.tier_b_calls)


@pytest.mark.parametrize('mode', ['oneshot', 'terminal', 'app'])
def test_launch_maintenance_uses_environment_plane_in_every_mode_F1402(mode):
    """Metadata/log streaming may use the container; ordinary exec may not."""
    records = []
    mod = _load_run_module()
    base = _make_fake(records)

    class NoRawExec(_FakeContainer):
        def exec_run(self, *_args, **_kwargs):
            raise AssertionError('nonstream command bypassed environment plane')

    class Environment(base):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.container = NoRawExec()
            self._rec['maintenance'] = []

        def exec(self, command, **kwargs):
            if kwargs.get('user') in ('root', 'coder'):
                self._rec['maintenance'].append({'cmd': command, **kwargs})
                return 0, 'OK'
            return super().exec(command, **kwargs)

    async def ready(*_args, **_kwargs):
        return True

    mod.DockerEnvironment = Environment
    mod._wait_for_ready = ready
    result = run_generator_callable(mod.run, config={'settings.set_yaml': {
        'mode': mode, 'command': 'echo task', 'sudo': True,
        'startup_commands': 'echo startup'}}, accounts=_GIT,
        timeout=10, label=_LABEL, ui_files=_UI_FILES)
    assert result.ok, result.error
    calls = records[-1]['maintenance']
    credentials = [row for row in calls if 'COLLIMATE_GIT_CRED' in (row.get('env') or {})]
    assert len(credentials) == 1
    assert credentials[0]['user'] == ('root' if mode == 'oneshot' else 'coder')
    assert 'ghp_s3cret' not in json.dumps(credentials[0]['cmd'])
    assert 'ghp_s3cret' not in json.dumps(records[-1].get('environment', {}))
    assert not any('ghp_s3cret' in line for line in result.logs)
    assert records[-1]['closed']
    if mode == 'terminal':
        assert any('/workspace rev-parse' in json.dumps(row['cmd']) for row in calls)
        assert any('/etc/sudoers.d/collimate' in json.dumps(row['cmd']) for row in calls)
    if mode == 'app':
        release = next(row for row in calls if mod._GIT_LOGIN_READY in json.dumps(row['cmd']))
        assert calls.index(credentials[0]) < calls.index(release)


@pytest.mark.parametrize('return_code', [0, 127])
def test_launch_environment_result_preserves_credentials_and_sudo_feedback_F1402(
        monkeypatch, return_code):
    logs = []
    mod = _load_run_module()
    monkeypatch.setattr(mod, 'gen', types.SimpleNamespace(log=logs.append))

    class Environment:
        def exec(self, command, **kwargs):
            if kwargs.get('env'):
                return return_code, 'failure leaked ghp_s3cret\n'
            return 0, 'OK'

    mod._configure_git(Environment(), account=_bound(_GIT['git'], monkeypatch))
    mod._enable_sudo(Environment())
    mod._release_startup_commands(Environment())
    assert any('Enabled passwordless sudo' in line for line in logs)
    assert not any('ghp_s3cret' in line for line in logs)
    assert any('Configured the Git account' in line for line in logs) == (return_code == 0)
    assert any('were not configured (exit 127)' in line for line in logs) == (return_code == 127)


@pytest.mark.parametrize('grant', [{}, {'update_packages': False}, {'internet': 'none'}])
def test_launch_resolves_code_server_only_when_declared__F1951(monkeypatch, tmp_path, grant):
    import _env_shared.upstream as upstream
    from collimate.permissions import Grant
    from _env_shared import derive_image_tag
    mod = _load_run_module()
    dockerfile = tmp_path / 'Dockerfile'
    dockerfile.write_text('ARG CODE_SERVER_VERSION=4.140.0\nFROM codercom/code-server:${CODE_SERVER_VERSION}\n')
    monkeypatch.setattr(gen, 'grant', lambda: Grant.from_dict(grant))
    monkeypatch.setattr(gen, 'packages', lambda: {})
    monkeypatch.setattr(gen, 'log', lambda message: None)
    monkeypatch.setattr(upstream, '_read_record', lambda: {})
    monkeypatch.setattr(upstream, '_write_record', lambda record: None)
    fetched = []
    def latest(source, github_token=None):
        fetched.append(source)
        return '4.141.0'
    monkeypatch.setattr(upstream, 'latest_version', latest)
    monkeypatch.setattr(git_account, 'release_token', lambda log=None: None)
    args = mod._requested_versions(str(dockerfile))
    allowed = gen.grant().update_packages and gen.grant().internet != 'none'
    assert args == ({'CODE_SERVER_VERSION': '4.141.0'} if allowed else {})
    assert fetched == (['github:coder/code-server'] if allowed else [])
    monkeypatch.setattr(gen, 'packages', lambda: {'code-server': '4.140.0'})
    pinned = mod._requested_versions(str(dockerfile))
    assert pinned == {'CODE_SERVER_VERSION': '4.140.0'}
    monkeypatch.setattr(gen, 'config_folder_path', lambda name: str(tmp_path))
    spec = mod._resolve_image('ignored:latest')
    assert spec['build_args'] == pinned
    assert spec['build_context'] == str(tmp_path)
    assert spec['image'] == derive_image_tag('launch-build', str(tmp_path), build_args=pinned)
    assert len(fetched) == int(allowed)
    assert derive_image_tag('launch-build', str(tmp_path), build_args=pinned) != derive_image_tag(
        'launch-build', str(tmp_path), build_args={'CODE_SERVER_VERSION': '4.141.0'})
    monkeypatch.setattr(gen, 'packages', lambda: {})
    monkeypatch.setattr(upstream, '_read_record', lambda: {
        'github:coder/code-server': {'version_seen': '4.139.0', 'checked_at': 10**12}})
    assert mod._requested_versions(str(dockerfile)) == {'CODE_SERVER_VERSION': '4.139.0'}
    dockerfile.write_text('FROM scratch\n')
    assert mod._requested_versions(str(dockerfile)) == {}


@pytest.mark.parametrize('mode', ['oneshot', 'terminal'])
def test_staged_scripts_keep_managed_python_writable_after_login__F1967(mode):
    result, records = _run_oneshot(
        {'mode': mode, 'command': 'python3 -c "import synthetic_dependency"'},
        config_folders={'scripts': {'01_setup.sh': 'python3 -m pip install -r requirements.txt\n'}},
    )
    assert result.ok, result.error
    if mode == 'oneshot':
        command = records[-1]['execs'][0]
    else:
        placed = next(row for row in result.tier_b_calls if row['op'] == 'start_terminal')
        assert placed['command'][:2] == ['bash', '-lc']
        command = placed['command'][2]
    assert 'if [ -x /opt/collimate-python/bin/python ]' in command
    assert '/tmp/collimate-launch-python-$(id -u)' in command
    assert '-m venv' in command
    assert 'export PATH=' in command
    assert command.index('export PATH=') < command.index('[scripts]') < command.index('import synthetic_dependency')


def test_oneshot_preview_reuses_scripts_python_environment__F1967(bridge_hook):
    _stop_after(bridge_hook)
    result, records = _run_oneshot(
        {'mode': 'oneshot', 'command': 'python3 -c "import synthetic_dependency"',
         'serve_command': 'python3 -m http.server 8080', 'serve_port': 8080},
        config_folders={'scripts': {'01_setup.sh': 'python3 -m pip install -r requirements.txt\n'}},
    )
    assert result.ok, result.error
    [preview] = records[-1]['bg']
    assert 'export PATH=' in preview
    assert '[scripts]' not in preview
    assert preview.endswith('python3 -m http.server 8080')


@pytest.mark.parametrize('maps, expected', [('absent', '4242:4343'), ('mapped', '0:0'), ('unreadable', None)])
def test_actual_owner_probe_distinguishes_absent_from_failed_maps__P0F28(tmp_path, monkeypatch, maps, expected):
    """Execute the actual probe shell; only missing kernel files mean identity."""
    import subprocess
    mod = _load_run_module()
    uid, gid = tmp_path / 'uid_map', tmp_path / 'gid_map'
    if maps == 'mapped':
        uid.write_text('0 4242 1\n')
        gid.write_text('0 4343 1\n')
    elif maps == 'unreadable':
        uid.mkdir()  # Present but cat fails: this must not become identity.
        gid.write_text('0 4343 1\n')
    script = mod._ID_MAPS_SCRIPT.replace('/proc/self/uid_map', str(uid)).replace('/proc/self/gid_map', str(gid))
    monkeypatch.setattr(mod, '_ID_MAPS_SCRIPT', script)
    logs = []
    monkeypatch.setattr(mod, 'gen', types.SimpleNamespace(log=logs.append))
    class Shell:
        def exec(self, argv, **kwargs):
            result = subprocess.run(argv, capture_output=True, text=True, timeout=2)
            return result.returncode, result.stdout + result.stderr
    assert mod._workspace_owner(Shell(), (4242, 4343)) == expected
    if maps == 'unreadable':
        assert any('could not read' in line for line in logs)


@pytest.mark.parametrize('boundary', ['freeze', '_hand_back_workspace'])
def test_app_retries_handback_cancelled_at_await_before_closing__P0F28(monkeypatch, boundary):
    """A real Task cancellation after a worker returns must not lose ownership."""
    import asyncio
    mod = _load_run_module()
    records = []
    mod.DockerEnvironment = _make_fake(records)
    _, patch = _owner_seen_at(records, (4242, 4343))
    patch(mod)
    async def ready(port, timeout=120):
        return True
    mod._wait_for_ready = ready
    real_to_thread = asyncio.to_thread
    cancelled = False
    async def interrupted(function, *args, **kwargs):
        nonlocal cancelled
        result = await real_to_thread(function, *args, **kwargs)
        if getattr(function, '__name__', '') == boundary and not cancelled:
            cancelled = True
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        return result
    monkeypatch.setattr(asyncio, 'to_thread', interrupted)
    result = run_generator_callable(
        mod.run, config={'settings.set_yaml': {'mode': 'app'}},
        timeout=10, label=_LABEL, ui_files=_UI_FILES,
    )
    assert cancelled and not result.ok and result.error == 'CancelledError'
    timeline = records[-1]['timeline']
    assert timeline.count('freeze') == 2, timeline
    assert timeline.count('handback') == (1 if boundary == 'freeze' else 2), timeline
    assert timeline[-1] == 'close', timeline


def test_app_stop_caps_actual_handback_command_and_still_closes__P0F28(monkeypatch):
    """Run a real timed shell command behind the app's exec seam, not a fake clock."""
    import subprocess
    import time
    mod = _load_run_module()
    records = []
    base = _make_fake(records)
    durations = []
    class Timed(base):
        def exec(self, command, **kwargs):
            if kwargs.get('user') == '0:0' and '/proc/self/uid_map' not in command[-1]:
                timeout = kwargs.get('timeout')
                assert timeout is None, timeout
                assert command[:3] == ["timeout", "--kill-after=1", "5.0"], command
                started = time.monotonic()
                import shlex
                child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
                work = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(child)}"
                process = subprocess.Popen([*command[:-1], work],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, start_new_session=True)
                try:
                    output, _ = process.communicate(timeout=8)
                    durations.append(time.monotonic() - started)
                    records[-1]['timeline'].append('handback-timeout')
                    return process.returncode, output
                finally:
                    import signal
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            return super().exec(command, **kwargs)
    mod.DockerEnvironment = Timed
    _, patch = _owner_seen_at(records, (4242, 4343))
    patch(mod)
    async def ready(port, timeout=120):
        return True
    mod._wait_for_ready = ready
    result = run_generator_callable(
        mod.run, config={'settings.set_yaml': {'mode': 'app'}},
        timeout=12, label=_LABEL, ui_files=_UI_FILES,
    )
    assert result.ok, result.error
    assert len(durations) == 1 and 0 < durations[0] < 8, (durations, result.logs)
    assert any('could not hand /workspace back' in line for line in result.logs), result.logs
    assert records[-1]['timeline'][-2:] == ['handback-timeout', 'close']


def test_app_closes_when_outer_handback_retry_freeze_raises__P0F28():
    """Both hand-back attempts fail; the outer close still owns cleanup."""
    mod = _load_run_module()
    records = []
    base = _make_fake(records)

    class RefusedFreeze(base):
        def freeze(self):
            super().freeze()
            attempts = records[-1]['timeline'].count('freeze')
            raise RuntimeError(f'freeze attempt {attempts} refused')

    mod.DockerEnvironment = RefusedFreeze
    _, patch = _owner_seen_at(records, (4242, 4343))
    patch(mod)

    async def ready(port, timeout=120):
        return True

    mod._wait_for_ready = ready
    result = run_generator_callable(
        mod.run, config={'settings.set_yaml': {'mode': 'app'}},
        timeout=10, label=_LABEL, ui_files=_UI_FILES,
    )
    assert not result.ok and 'freeze attempt 2 refused' in (result.error or ''), result.error
    assert records[-1]['timeline'][-3:] == ['freeze', 'freeze', 'close']
    assert records[-1]['handbacks'] == []
