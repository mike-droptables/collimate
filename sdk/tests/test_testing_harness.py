# SPDX-License-Identifier: MIT
"""Tests for the host-free run engine (:mod:`collimate.testing`)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from collimate.testing import (
    run_generator,
    run_generator_callable,
)

from tests._workspace import GENERATORS


_REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Options declaration fixture and actual input bytes are tested in test_options_stack.py.
# ---------------------------------------------------------------------------


def test_missing_entrypoint_is_a_failed_result_not_an_exception():
    res = run_generator(GENERATORS / "does_not_exist.py")
    assert res.ok is False
    assert "not found" in (res.error or "")


# ---------------------------------------------------------------------------
# Failure surfaces as a result (not a raised exception)
# ---------------------------------------------------------------------------


def test_broken_generator_reports_traceback():
    async def boom():
        raise ValueError("kaboom")

    res = run_generator_callable(boom)
    assert res.ok is False
    assert "kaboom" in (res.error or "")
    assert res.traceback and "ValueError" in res.traceback


def test_raise_for_status_raises_on_failure():
    res = run_generator_callable(lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    assert res.ok is False
    try:
        res.raise_for_status()
    except RuntimeError as e:
        assert "nope" in str(e)
    else:  # pragma: no cover
        raise AssertionError("raise_for_status should have raised")


def test_timeout_bounds_a_hanging_async_run():
    async def hang():
        import gen  # noqa: F401

        while True:
            await asyncio.sleep(0.05)

    res = run_generator_callable(hang, timeout=0.5)
    assert res.ok is False
    assert "timed out" in (res.error or "")


def test_inner_timeout_error_is_not_reported_as_the_run_timeout():
    async def run():
        import gen  # noqa: F401

        await asyncio.wait_for(asyncio.sleep(1), 0.01)

    res = run_generator_callable(run)
    assert res.ok is False
    assert "timed out after" not in (res.error or "")
    assert res.traceback is not None


def test_sync_timeout_error_keeps_its_message_and_traceback():
    def run():
        raise TimeoutError("socket read timed out")

    res = run_generator_callable(run)
    assert res.ok is False
    assert "socket read timed out" in (res.error or "")
    assert res.traceback is not None


def test_keyboard_interrupt_propagates():
    def run():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_generator_callable(run)


def test_config_seeding_writes_text_verbatim_and_rejects_non_maps():
    seen = {}

    def run():
        import gen

        seen["yaml"] = gen.config("a.set_yaml")
        # gen.config only answers maps; a raw file is seeded as given.
        raw = Path(gen.workspace_path()) / ".collrun" / "config" / "files" / "n.txt"
        seen["raw"] = raw.read_bytes().decode("utf-8")

    res = run_generator_callable(run, config={"a.set_yaml": "model: x", "n.txt": "hello"})
    assert res.ok, res.error
    assert seen == {"yaml": {"model": "x"}, "raw": "hello"}

    res = run_generator_callable(run, config={"a.set_yaml": ["x"]})
    assert res.ok is False
    assert "workspace seeding failed" in (res.error or "")


# ---------------------------------------------------------------------------
# Daemon generators validate structurally via tier_b_calls / endpoints
# ---------------------------------------------------------------------------


def test_daemon_serve_port_is_captured_and_autocancelled():
    async def daemon():
        import gen

        gen.update_file("server.md", "up")
        await gen.serve_port(8080, name="preview")
        await gen.commit(name="preview")
        # A real daemon loops until Stop; the harness flips cancelled after
        # serve_port so this exits.
        while not gen.cancelled:
            await asyncio.sleep(0.05)

    res = run_generator_callable(daemon, timeout=10)
    assert res.ok, res.error
    assert any(c["op"] == "serve_port" and c["port"] == 8080 for c in res.tier_b_calls)
    assert any(e.kind == "http" and e.port == 8080 for e in res.endpoints)


# ---------------------------------------------------------------------------
# Inputs / config / prompt / secrets / scripted messages
# ---------------------------------------------------------------------------


def test_inputs_and_prompt_are_visible():
    def run():
        import gen

        files = gen.list_files()
        gen.update_file("seen.txt", f"{sorted(files)}|{gen.prompt}")

    res = run_generator_callable(run, inputs={"a.txt": "1", "b.txt": "2"}, prompt="hello")
    assert res.ok, res.error
    body = res.files["seen.txt"]
    assert "a.txt" in body and "b.txt" in body and "hello" in body


def test_config_values_are_read():
    def run():
        import gen

        cfg = gen.config("pydantic-config.set_yaml")
        gen.update_file("model.txt", str(cfg.get("model", "MISSING")))

    res = run_generator_callable(
        run, config={"pydantic-config.set_yaml": {"model": "claude-x"}}
    )
    assert res.ok, res.error
    assert res.files["model.txt"] == "claude-x"


def test_account_resolves_from_the_accounts_fixture():
    async def run():
        import gen

        token = gen.account("service")["secrets"]["access_token"]
        gen.update_file("tok.txt", token)

    res = run_generator_callable(run, accounts={"service":{"secrets":{"access_token":"s3cret"}}})
    assert res.ok, res.error
    assert res.files["tok.txt"] == "s3cret"


def test_scripted_messages_drive_serve_chat_then_stop():
    """A host-free ``serve_chat`` run: the scripted ``messages`` queue feeds
    ``chat_poll`` as user turns; the handler records each, and the run exits
    cleanly when the queue drains (the harness flips cancelled)."""
    async def run():
        import gen

        seen = []

        async def on_turn(turn):
            seen.append(turn.user_msg)
            gen.update_file("chat.txt", ",".join(seen))

        await gen.serve_chat(on_turn, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=["hi", "bye"], timeout=10)
    assert res.ok, res.error
    assert res.files["chat.txt"] == "hi,bye"


def test_serve_chat_mirrors_running_and_awaiting_input_phases(bridge_hook):
    """serve_chat emits ``awaiting_input`` when it parks idle and ``running``
    when a turn starts — deduped per transition, never per poll tick."""
    async def run():
        import gen

        async def on_turn(turn):
            turn.append("assistant", "ok")

        await gen.serve_chat(on_turn, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=["hi"], timeout=10)
    assert res.ok, res.error
    names = [p for p, _ in bridge_hook["bridge"].phases]
    # No seeded first turn → the session parks idle from the start; the
    # scripted message then flips it to running.
    assert names[0] == "awaiting_input"
    assert "running" in names
    # Deduped: the 0.2s poll loop must not re-emit the same phase.
    assert all(a != b for a, b in zip(names, names[1:]))


def test_serve_chat_parks_awaiting_input_with_no_messages(bridge_hook):
    """A chat that never receives a message still reports the parked state."""
    async def run():
        import gen

        async def on_turn(turn):
            turn.append("assistant", "ok")

        await gen.serve_chat(on_turn, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=[], timeout=10)
    assert res.ok, res.error
    assert [p for p, _ in bridge_hook["bridge"].phases] == ["awaiting_input"]


def _annotation_listener():
    """A run serving a broadcast inbox whose companion subscribes to the
    annotation context, pulls each batch once the session is idle, turns it
    into one ``@annotations`` turn, and answers the batch's last root."""
    async def run():
        import gen

        async def on_turn(turn):
            roots = [m for m in turn.batch if m.reply_to is None]
            await gen.annotations.post(f"handled {len(turn.batch)}", reply_to=roots[-1].id)

        wake = asyncio.Event()

        def on_ready():
            async def watch():
                with gen.annotations.subscribe(lambda notice: wake.set()):
                    while not gen.cancelled:
                        try:
                            await asyncio.wait_for(wake.wait(), timeout=0.3)
                        except TimeoutError:
                            pass
                        wake.clear()
                        if gen.session.chat_busy() or not gen.annotations.notice.unread:
                            continue
                        batch = await gen.annotations.pull()
                        if batch:
                            gen.session.submit_turn("\n".join(m.text for m in batch), source="@annotations")
                            pending.append(batch)
            asyncio.ensure_future(watch())

        pending: list = []

        async def handler(turn):
            turn.batch = pending.pop(0)
            await on_turn(turn)

        await gen.serve_chat(handler, files=False, broadcast=True, on_ready=on_ready,
                             pointer_file=gen.run_path("chat/conversation.chat"),
                             threads_dir=gen.run_path("chat"))
    return run


def test_scripted_annotations_drive_a_listening_run_then_stop(bridge_hook):
    res = run_generator_callable(_annotation_listener(), messages=[], timeout=15, annotations=[
        "fix the title", {"text": "and the footer", "mode": "interrupt"}])
    assert res.ok, res.error
    bridge = bridge_hook["bridge"]
    users = [a for a in res.annotations if a.author == "user"]
    assert [a.text for a in users] == ["and the footer", "fix the title"]
    assert all(a.read_at for a in users)
    [post] = [a for a in res.annotations if a.author == "run"]
    assert post.text == "handled 2" and post.reply_to == users[-1].id
    assert bridge.annotation_posts == [{"id": post.id, "text": "handled 2",
                                        "reply_to": users[-1].id}]
    assert bridge.annotations_listening is True


def test_unread_scripted_annotations_hold_the_auto_stop():
    """A served chat whose generator never pulls its scripted annotations is
    never idle: the run times out instead of stopping as if they were read."""
    async def run():
        import gen

        async def on_turn(turn):  # pragma: no cover — no turn is sent
            pass

        await gen.serve_chat(on_turn, files=False, pointer_file=gen.run_path("chat/conversation.chat"),
                             threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=[], annotations=["waiting"], timeout=1.5)
    assert not res.ok and res.error.startswith("timed out")


def test_add_annotation_mid_run_advances_interrupt_seq(tmp_path):
    from collimate.testing import make_test_bridge

    bridge = make_test_bridge(tmp_path)
    try:
        before = bridge.annotations_poll()
        assert (before.unread, before.interrupt_seq) == (0, 0)
        bridge.add_annotation("stop that", mode="interrupt")
        after = bridge.annotations_poll()
        assert (after.unread, after.interrupt_seq) == (1, 1)
        assert after.rev > before.rev
        bridge.add_annotation("later")
        assert bridge.annotations_poll().interrupt_seq == 1
    finally:
        bridge.close()


def test_result_annotations_hold_the_whole_context():
    """The result carries every message — the seeded prompt, the user's and
    the run's — whether or not the run read them."""
    async def run():
        import gen

        await gen.annotations.post("hello from the run")

    res = run_generator_callable(run, prompt="Do the thing", annotations=["unread one"])
    assert res.ok, res.error
    assert [(a.kind, a.author, a.text, a.read_at is not None) for a in res.annotations] == [
        ("prompt", "user", "Do the thing", True),
        ("message", "user", "unread one", False),
        ("message", "run", "hello from the run", True),
    ]


def _cancel_probe():
    """A ``serve_chat`` run whose turn reports whether it was cancelled."""
    async def run():
        import gen

        async def on_turn(turn):
            for _ in range(40):
                if turn.cancelled:
                    break
                await asyncio.sleep(0.02)
            gen.update_file("cancelled.txt", str(turn.cancelled))

        await gen.serve_chat(on_turn, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    return run


def test_interrupt_cancels_only_its_own_thread():
    """An interrupt names the thread whose turn it stops."""
    res = run_generator_callable(
        _cancel_probe(),
        messages=["hi", {"kind": "interrupt", "thread_id": "main"}],
        timeout=10,
    )
    assert res.ok, res.error
    assert res.files["cancelled.txt"] == "True"


def test_interrupt_without_thread_id_is_ignored():
    """An unscoped interrupt is malformed — it is dropped, NOT applied to
    whatever turn happens to be in flight."""
    res = run_generator_callable(
        _cancel_probe(),
        messages=["hi", {"kind": "interrupt"}, {"kind": "interrupt", "thread_id": "other"}],
        timeout=10,
    )
    assert res.ok, res.error
    assert res.files["cancelled.txt"] == "False"


def test_thefinal_flush_failing_fails_the_run__F1135__P6F01(bridge_hook):
    """F1135: the harness and ``gen.main`` share one rule — a final flush
    that committed nothing fails the run."""
    from collimate._host.bridge import IPCDisconnectedError

    def gone(**_kw):
        raise IPCDisconnectedError("host closed IPC channel")

    bridge_hook["setup"] = lambda b: setattr(b, "update_cell", gone)

    async def run():
        import gen

        gen.update_file("out.md", "o")

    res = run_generator_callable(run)
    assert not res.ok
    assert "final flush failed" in (res.error or "")


def test_a_partly_refusedfinal_flush_keeps_the_run__F1135(bridge_hook):
    captured = {}
    bridge_hook["setup"] = lambda bridge: captured.update(bridge=bridge)
    """A partial refusal committed the rest: the run stands, under the
    harness as under ``gen.main``."""
    async def run():
        import gen

        gen.update_file("out.md", "o")
        (Path(gen.workspace_path()) / "_runs" / "sib-1").mkdir(parents=True)
        (Path(gen.workspace_path()) / "_runs" / "sib-1" / "x.md").write_text("x")
        from collimate.native import require_bridge
        require_bridge().tracked_writes.add("_runs/sib-1/x.md")

    res = run_generator_callable(run)
    assert res.ok, res.error
    assert captured["bridge"].updates[-1]["files"] == {"out.md": "o"}


def test_a_run_never_pins_its_own_cell():
    """A run shows a file with ``gen.reveal_file``; its updates carry only
    files and a name — the cell's pin is the user's."""
    async def run():
        import gen

        gen.update_file("page.md", "# page\n")
        await gen.commit(name="Page")
        await gen.reveal_file("page.md")

    res = run_generator_callable(run)
    assert res.ok, res.error
    assert res.name == "Page"
    assert [c for c in res.tier_b_calls if c["op"] == "reveal_file"] == [
        {"op": "reveal_file", "path": "page.md", "committed": True}]


# ---------------------------------------------------------------------------
# gen.packages() under the harness: the runner's pins, {} when none — the
# value a harness image's version lookup reads, so a live test floats to the
# latest release unless it pins one (run_generator_callable(packages=…)).
# ---------------------------------------------------------------------------


def test_packages_default_to_no_pins_and_round_trip():
    async def run():
        import gen

        gen.update_file("pins.json", json.dumps(gen.packages()))
        await gen.commit(name="pins")

    res = run_generator_callable(run)
    assert res.ok, res.error
    assert json.loads(res.files["pins.json"]) == {}

    res = run_generator_callable(run, packages={"codex": "0.150.0"})
    assert res.ok, res.error
    assert json.loads(res.files["pins.json"]) == {"codex": "0.150.0"}


def test_create_closes_its_self_pipe_when_construction_fails(tmp_path):
    """A bad foreign_runs label raises out of create(); the half-built
    bridge's cancellation pipe must not leak."""
    import os

    from collimate.testing._bridge import RecordingBridge

    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("needs /proc/self/fd")
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(ValueError):
        RecordingBridge.create(str(tmp_path), label="x", foreign_runs={"x": ["a.txt"]})
    assert len(os.listdir("/proc/self/fd")) == before
