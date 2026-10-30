# SPDX-License-Identifier: MIT
"""LIVE (app): a CLI run whose harness CLI runs on this machine, through the
real app and browser — Claude Code on a subscription entry, Antigravity on
the Google plan, each with nothing pasted (the CLI signs in itself).

Per run: Verify says the entry's CLI runs on this machine; the runner (the
harness's shipped config on the CLI surface, ``docker: none``, ``commands:
ask``) starts; the terminal pointer appears and the browser's terminal
renderer mounts and attaches, which spawns the PTY child: this machine's
CLI, labelled with the run (``COLLIMATE_HOST_TERMINAL``), in the run's
workspace, out of the entry's per-account home under the host cache; the
run's Stop ends it with its whole process group. A second run on the same
entry runs out of the same home, where the sign-in, once the user makes it
in the TTY, persists. No sign-in happens here: that is the user's, in a
browser (``sdk/tests_live/README.md``, the manual checklist), so nothing is
billed.

Gate: the harness's CLI on this machine (``collimate.host_harness.probe``);
the server inherits this process's PATH, so an ``agy`` the test's PATH leads
to is found too.
"""
from __future__ import annotations

import os
import shutil
import time
import uuid

import pytest

from collimate import host_harness

from tests_live.conftest import make_runner

pytestmark = pytest.mark.live

_LABEL = b"COLLIMATE_HOST_TERMINAL="
_PLANS = {
    "claude-code": {"service": "anthropic", "auth_type": "subscription", "plan": "claude_code", "model": "haiku"},
    "antigravity": {"service": "gemini", "auth_type": "cli", "plan": "google", "model": "default"},
}


def _children(run_id: str) -> dict:
    """``{pid: environ}`` of this machine's processes labelled with the run."""
    found = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/environ", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        env = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
        label = env.get(b"COLLIMATE_HOST_TERMINAL", b"").decode()
        if label.endswith(":" + run_id):
            found[int(name)] = env
    return found


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


@pytest.mark.parametrize("harness", ["claude-code", "antigravity"])
def test_a_cli_run_on_this_machine_runs_in_its_entrys_own_home_and_stops(api, cell_view, harness):
    found = host_harness.probe(harness)
    if found is None or not found.usable:
        pytest.skip(f"no usable {host_harness.HARNESS_HOST_BINARY[harness]} on this machine")
    plan = _PLANS[harness]
    base = api.request("POST", "/api/accounts", {
        "name": f"{harness} plan live", "kind": "account", "service": plan["service"],
        "auth_type": plan["auth_type"], "protocol": "cli",
        "metadata": {"harness": harness, "plan": plan["plan"]}, "secrets": {}})
    model = api.request("POST", "/api/accounts", {
        "name": f"{harness} model live", "kind": "model", "credential_account_id": base["id"],
        "model": plan["model"], "metadata": {"harness": harness}})
    runner_ids = []
    homes = []
    try:
        assert base["configured"] is True and model["configured"] is True, (base, model)
        draft = {key: value for key, value in model.items()
                 if key not in {"configured", "default_model", "generation", "secret_fields",
                                "supported_harnesses"}}
        check = api.request("POST", "/api/accounts/check", {**draft, "secrets": {}}, timeout=90)
        assert check["valid"] is True, check
        assert f"its CLI runs on this machine ({found.binary} {found.version} at {found.path})" in check["message"]
        for attempt in (1, 2):
            runner = make_runner(api, harness, model["id"], f"{harness} cli live {uuid.uuid4().hex[:6]}",
                                 generator="agent", settings={"surface": "terminal"},
                                 permissions={"docker": "none", "commands": "ask"})
            runner_ids.append(runner["id"])
            cell = api.new_cell(f"live-{harness}-cli-{attempt}", [])
            run = api.start_runner(cell, runner)
            pointer = api.run_path(run, f"terminal/{harness}.terminal")
            api.wait_until(lambda: pointer in api.cell_paths(cell), timeout=240, every=2,
                           label="the terminal pointer to be written", run_id=run)
            view = cell_view(cell).reopen()
            api.wait_until(view.terminal_mounted, timeout=120, every=2,
                           label=f"the {harness} terminal to mount", run_id=run)
            api.wait_until(lambda: _children(run), timeout=60, every=1,
                           label="the CLI to start on this machine", run_id=run)
            children = _children(run)
            leader = min(children)
            env = children[leader]
            exe = os.path.realpath(f"/proc/{leader}/exe")
            assert exe == os.path.realpath(found.path) or os.path.basename(found.path) in open(
                f"/proc/{leader}/cmdline", "rb").read().decode(errors="replace"), exe
            home = (env.get(b"CLAUDE_CONFIG_DIR") if harness == "claude-code" else env.get(b"HOME")).decode()
            cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
            assert home == os.path.join(cache, "collimate", "host-homes", harness, base["id"]), home
            assert not {b"GEMINI_API_KEY", b"ANTHROPIC_API_KEY", b"CLAUDE_CODE_OAUTH_TOKEN"} & set(env)
            homes.append(home)
            time.sleep(8)                   # let the TUI draw its first screen
            view.shot(f"{harness}-cli-{attempt}")
            api.stop_run(run)
            pids = list(children)
            api.wait_until(lambda: not any(_alive(pid) for pid in pids), timeout=60, every=1,
                           label="the CLI's process group to end with the run", run_id=run)
        assert homes[0] == homes[1]
    finally:
        api.stop_started_runs()
        for runner_id in runner_ids:
            try:
                api.request("DELETE", f"/api/runners/{runner_id}")
            except Exception:  # noqa: BLE001 — cleanup never masks the verdict
                pass
        for identity in (model["id"], base["id"]):
            try:
                api.request("DELETE", f"/api/accounts/{identity}")
            except Exception:  # noqa: BLE001
                pass
        # The entry was this test's own: its home goes with it.
        for home in set(homes):
            if os.path.basename(home) == base["id"]:
                shutil.rmtree(home, ignore_errors=True)
