# SPDX-License-Identifier: MIT
"""Coverage for the terminal daemon helpers: ``gen.serve_terminal`` (register
PTY + write and reveal the canonical ``.terminal`` pointer in the run folder +
stop the PTY and delete the pointer on exit) and ``gen.actions`` (the action
loop that ends itself on run cancellation).

These absorb the boilerplate every terminal-serving generator used to copy —
and the footguns that came with it: a hand-rolled pointer dict drifting from
the renderer's contract, a forgotten ``stop_terminal`` in a ``finally``, and a
pointer left behind (or written outside the run folder).
"""
from __future__ import annotations

import json

import pytest

from collimate.pointers import TERMINAL_KEYS, terminal_pointer
from collimate.testing import run_generator_callable

from tests._workspace import GENERATORS


def _ops(res, op):
    return [c for c in res.tier_b_calls if c["op"] == op]


def test_serve_terminal_registers_reveals_stops_and_removes_its_pointer(bridge_hook):
    seen: dict = {}
    bridge_hook["setup"] = lambda bridge: seen.update(bridge=bridge)

    async def run():
        import gen

        pointer = gen.run_path("shell.terminal")
        seen["pointer"] = pointer
        async with gen.serve_terminal(
            "cid-1", ["bash"], cwd="/work",
            label="Shell", pointer=pointer,
            name="Container",
        ) as session:
            assert session["terminal_id"]
            assert gen.has_file(pointer)

    res = run_generator_callable(run)
    assert res.ok, res.error
    pointer = seen["pointer"]

    start = _ops(res, "start_terminal")
    stop = _ops(res, "stop_terminal")
    assert len(start) == 1 and start[0]["command"] == ["bash"]
    # The PTY registered by the block is the one stopped on exit.
    assert len(stop) == 1 and stop[0]["terminal_id"] == "rec-term-1"

    # Pointer file: canonical shape, flushed with the name, then revealed —
    # after the flush, so the host can show it.
    ptr = json.loads(next(update["files"][pointer] for update in seen["bridge"].updates
                              if pointer in update["files"]))
    assert set(TERMINAL_KEYS) <= set(ptr)
    assert ptr["label"] == "Shell"
    assert "buttons" not in ptr  # only present when requested
    assert any(update["name"] == "Container" for update in seen["bridge"].updates)
    assert _ops(res, "reveal_file") == [
        {"op": "reveal_file", "path": pointer, "committed": True}]
    # On exit the pointer is gone from the run's files.
    assert pointer not in res.files


def test_serve_terminal_buttons_land_in_the_pointer():
    async def run():
        import gen

        pointer = gen.run_path("claude.terminal")
        async with gen.serve_terminal(
            "cid-1", ["claude"],
            label="Claude", pointer=pointer,
            buttons=[{"id": "capture_chat", "label": "Capture Chat"}],
        ):
            gen.update_file("seen.json", gen.read_file(pointer))

    res = run_generator_callable(run)
    assert res.ok, res.error
    ptr = json.loads(res.files["seen.json"])
    assert ptr["buttons"] == [{"id": "capture_chat", "label": "Capture Chat"}]


@pytest.mark.parametrize("pointer, match", [
    ("app.terminal", "inside this run's folder"),
    ("_runs/other-run-1a2b3c4d/app.terminal", "inside this run's folder"),
    (None, "must end in .terminal"),
])
def test_serve_terminal_refuses_a_pointer_outside_the_run_folder(pointer, match):
    async def run():
        import gen

        target = pointer or gen.run_path("app.txt")
        with pytest.raises(ValueError, match=match):
            async with gen.serve_terminal("cid-1", ["bash"], pointer=target):
                pass  # pragma: no cover

    res = run_generator_callable(run)
    assert res.ok, res.error
    # Refused before any PTY was allocated.
    assert _ops(res, "start_terminal") == []


def test_serve_terminal_needs_its_pointer_type_declared():
    async def run():
        import gen

        with pytest.raises(ValueError, match="ui_files"):
            async with gen.serve_terminal("cid-1", ["bash"],
                                          pointer=gen.run_path("app.terminal")):
                pass  # pragma: no cover

    res = run_generator_callable(run, ui_files=[".chat"])
    assert res.ok, res.error
    assert _ops(res, "start_terminal") == []


def test_serve_terminal_writes_its_pointer_as_a_ui_file():
    async def run():
        import gen

        async with gen.serve_terminal("cid-1", ["bash"],
                                      pointer=gen.run_path("app.terminal")):
            assert gen.list_ui_files() == ["app.terminal"]
        assert gen.list_ui_files() == []

    res = run_generator_callable(run, ui_files=[".terminal"])
    assert res.ok, res.error


def test_serve_terminal_needs_a_pointer():
    async def run():
        import gen

        async with gen.serve_terminal("cid-1", ["bash"]):
            pass  # pragma: no cover

    res = run_generator_callable(run)
    assert res.ok is False and "pointer" in (res.error or "")


def test_serve_terminal_stops_the_pty_even_when_the_body_raises():
    seen: dict = {}

    async def run():
        import gen

        seen["pointer"] = gen.run_path("shell.terminal")
        async with gen.serve_terminal("cid-1", ["bash"], label="Shell",
                                      pointer=seen["pointer"]):
            raise ValueError("body exploded")

    res = run_generator_callable(run)
    assert res.ok is False and "body exploded" in (res.error or "")
    assert len(_ops(res, "stop_terminal")) == 1
    assert seen["pointer"] not in res.files


def test_actions_loop_ends_on_cancellation():
    # The recording bridge's start_terminal auto-signals stop (a daemon can't
    # otherwise end in a host-free test), so gen.actions() must terminate the
    # async-for on its own — no `while not gen.cancelled` in the generator.
    async def run():
        import gen

        seen = []
        async with gen.serve_terminal("cid-1", ["bash"], label="Shell",
                                      pointer=gen.run_path("shell.terminal")):
            async for action in gen.actions(poll=0.05):
                seen.append(action)  # pragma: no cover — no actions are queued
        assert seen == []

    res = run_generator_callable(run, timeout=10.0)
    assert res.ok, res.error


def test_terminal_pointer_button_shape_is_normalized():
    session = {"terminal_id": "s", "ws_path": "/ws/s", "token": "t"}
    ptr = terminal_pointer(session, label="X", buttons=[{"id": 1, "label": 2}])
    assert ptr["buttons"] == [{"id": "1", "label": "2"}]
    assert terminal_pointer(session).get("buttons") is None
    # A button's optional hover text and confirmation ride along; unknown
    # keys and empty values do not.
    ptr = terminal_pointer(session, buttons=[
        {"id": "capture_chat", "label": "Capture Chat", "title": "Saves the chat",
         "confirm": "Sure?", "icon": "x"},
        {"id": "b", "label": "B", "title": ""}])
    assert ptr["buttons"] == [
        {"id": "capture_chat", "label": "Capture Chat", "title": "Saves the chat",
         "confirm": "Sure?"},
        {"id": "b", "label": "B"}]


def test_serve_tty_until_ends_when_the_program_exits(bridge_hook):
    """A terminal leg parked on ``serve_tty_until`` must END when the PTY's
    program exits (``gen.terminal_status`` reports ``ended``) — the run
    would otherwise sit on a dead terminal — and say so in the run log."""
    # The recording bridge auto-signals Stop when a terminal starts (so a
    # daemon's `while not cancelled` loop exits in tests); here the loop
    # must end on the PROGRAM exiting, so keep the run alive.
    bridge_hook["setup"] = lambda b: setattr(b, "_autocancel_on_serve", False)

    async def run():
        import asyncio
        import sys

        import gen
        from collimate import native

        sys.path.insert(0, str(GENERATORS))
        from _agent_shared import serve_tty_until

        async with gen.serve_terminal("cid-1", ["claude"], label="Claude",
                                      pointer=gen.run_path("terminal/claude.terminal")) as tty:
            sid = tty["terminal_id"]

            async def _die():
                await asyncio.sleep(0.3)
                native.current_bridge()._terminals_ended.add(sid)   # the program exited

            asyncio.ensure_future(_die())
            reason = await asyncio.wait_for(
                serve_tty_until(terminal_id=sid, poll=0.1), timeout=5)
            gen.update_file("reason.txt", reason)

    res = run_generator_callable(run, timeout=15)
    assert res.ok, res.error
    assert res.files["reason.txt"] == "exited"
    assert any("terminal program ended" in line for line in res.logs)


@pytest.mark.parametrize('last', [
    {'exists': True, 'ended': True, 'exit_code': 23},
    {'exists': False, 'ended': True, 'exit_code': None},
])
def test_wait_terminal_exit_polls_off_loop_and_returns_observed_code__F2031(native_bridge, last):
    import asyncio
    import threading
    import gen
    thread = threading.get_ident()
    seen = []
    answers = iter([{'exists': True, 'ended': False, 'exit_code': None}, last])
    def status(terminal_id):
        seen.append((terminal_id, threading.get_ident()))
        return next(answers)
    native_bridge.terminal_status = status
    assert asyncio.run(gen.wait_terminal_exit('owned-terminal', poll=0.001)) == last['exit_code']
    assert [sid for sid, _ in seen] == ['owned-terminal'] * 2
    assert all(worker != thread for _, worker in seen)


def test_wait_terminal_exit_stop_and_host_failure_are_not_false_exit_codes__F2031(native_bridge):
    import asyncio
    import gen
    def failed(_):
        raise RuntimeError('host status failed')
    native_bridge.terminal_status = failed
    with pytest.raises(RuntimeError, match='host status failed'):
        asyncio.run(gen.wait_terminal_exit('owned'))
    native_bridge._signal_stop()
    assert asyncio.run(gen.wait_terminal_exit('stopped')) is None


def test_the_host_form_runs_on_this_machine_through_its_own_op(bridge_hook):
    """``container_id=None`` asks for a PTY on this machine: its own host op
    (the host checks the run's grant and placement), the env as the
    program's whole environment, the same pointer and teardown."""
    seen: dict = {}
    bridge_hook["setup"] = lambda bridge: seen.update(bridge=bridge)

    async def run():
        import gen

        pointer = gen.run_path("cli.terminal")
        async with gen.serve_terminal(
            None, ["/opt/bin/claude", "--session-id", "s"], cwd=gen.workspace_path(),
            env={"PATH": "/usr/bin", "HOME": "/home/me"}, label="Claude", pointer=pointer,
            keep_alive=True,
        ) as session:
            assert session["terminal_id"] and gen.has_file(pointer)

    res = run_generator_callable(run)
    assert res.ok, res.error
    assert _ops(res, "start_terminal") == []
    [start] = _ops(res, "start_host_terminal")
    assert start["command"] == ["/opt/bin/claude", "--session-id", "s"]
    assert start["env_names"] == ["HOME", "PATH"] and start["keep_alive"] is True
    assert len(_ops(res, "stop_terminal")) == 1
