"""O08: the finish frame carries the run's own verdict, ``note`` and ``outcome``.

``gen.finish(note=..., outcome=...)`` records it on the bridge; ``gen.main``'s
one finish frame carries it to the host, which stores it on the run row.
"""
from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager

import pytest


@pytest.fixture
def bridge(tmp_path):
    from collimate import native
    from collimate.testing import make_test_bridge
    b = make_test_bridge(tmp_path)
    b.sent = []
    b.emit = b.sent.append
    native.set_bridge(b)
    try:
        yield b
    finally:
        native._clear_bridge()
        b.close()


def test_the_host_bridge_forwards_note_and_outcome_O08(bridge):
    bridge.finish(success=True, note="  3 of 4 checks\n passed ", outcome="fail")
    assert bridge.sent == [{"op": "finish", "success": True, "error_message": "",
                            "note": "3 of 4 checks passed", "outcome": "fail"}]


def test_a_finish_without_a_verdict_sends_neither_field_O08(bridge):
    bridge.finish(success=False, error_message="boom")
    assert bridge.sent == [{"op": "finish", "success": False, "error_message": "boom"}]


def test_the_note_is_capped_like_failure_reason_O08(bridge):
    from collimate._host.ipc import FINISH_NOTE_MAX_CHARS
    bridge.finish(success=True, note="x" * 1000, outcome="pass")
    note = bridge.sent[0]["note"]
    assert FINISH_NOTE_MAX_CHARS == 300
    assert len(note) == FINISH_NOTE_MAX_CHARS and note.endswith("…")


def test_an_unknown_outcome_is_refused_before_anything_is_sent_O08(bridge):
    with pytest.raises(ValueError):
        bridge.finish(success=True, outcome="maybe")
    assert bridge.sent == []


def test_gen_finish_records_the_verdict_and_the_last_call_wins_O08(bridge):
    import gen
    gen.finish(note="first", outcome="pass")
    gen.finish(note="second\nline")
    assert (bridge.run_note, bridge.run_outcome) == ("second line", None)
    assert bridge.sent == [], "gen.finish does not end the run"


@pytest.mark.parametrize("kwargs, error", [
    ({"outcome": "passed"}, ValueError),
    ({"note": 3}, TypeError),
])
def test_gen_finish_refuses_a_bad_verdict_O08(bridge, kwargs, error):
    import gen
    with pytest.raises(error):
        gen.finish(**kwargs)
    assert (bridge.run_note, bridge.run_outcome) == (None, None)


def test_gen_finish_is_public_O08():
    import gen
    from collimate import native
    assert "finish" in native.__all__ and gen.finish is native.finish


@contextmanager
def _recording_host():
    """A localhost IPC peer that records every frame until the run hangs up."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(5.0)
    frames: list = []

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn, conn.makefile("r", encoding="utf-8") as lines:
            for line in lines:
                frames.append(json.loads(line))

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield srv.getsockname()[1], frames
    finally:
        t.join(5.0)
        srv.close()


def _hosted(monkeypatch, tmp_path, port):
    (tmp_path / ".collrun").mkdir(exist_ok=True)
    (tmp_path / ".collrun/file_revisions.json").write_text("{}")
    monkeypatch.setenv("COLLIMATE_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("COLLIMATE_IPC_PORT", str(port))
    monkeypatch.setenv("COLLIMATE_PERMISSIONS", "{}")
    monkeypatch.setenv("COLLIMATE_RUN_ID", "run-1")
    monkeypatch.setenv("COLLIMATE_CHANNEL", "chan")
    monkeypatch.setenv("COLLIMATE_RUNNER", json.dumps({
        "runner": None, "name": None, "label": "sub-run1", "folder": "_runs/sub-run1"}))
    monkeypatch.setenv("COLLIMATE_UI_FILES", "[]")


@pytest.mark.parametrize("raises", [False, True])
def test_main_sends_the_recorded_verdict_with_its_one_finish_O08(tmp_path, monkeypatch, raises):
    import gen
    with _recording_host() as (port, frames):
        _hosted(monkeypatch, tmp_path, port)

        async def run():
            gen.finish(note="probe answered", outcome="fail" if raises else "pass")
            if raises:
                raise RuntimeError("checker crashed")

        if raises:
            with pytest.raises(SystemExit):
                gen.main(run)
        else:
            gen.main(run)

    [finish] = [f for f in frames if f.get("op") == "finish"]
    assert finish["success"] is not raises
    assert (finish["note"], finish["outcome"]) == (
        "probe answered", "fail" if raises else "pass")


@pytest.mark.parametrize("raises", [False, True])
def test_the_harness_result_keeps_the_note_and_outcome_O08(raises):
    import gen
    from collimate.testing import run_generator_callable

    async def run():
        gen.finish(note="  three checks,\n one failed ", outcome="fail")
        if raises:
            raise RuntimeError("checker crashed")

    res = run_generator_callable(run)
    assert res.ok is not raises
    assert (res.note, res.outcome) == ("three checks, one failed", "fail")


def test_the_harness_result_has_no_verdict_when_none_was_sent_O08():
    from collimate.testing import run_generator_callable

    res = run_generator_callable(lambda: None)
    assert res.ok and res.note is None and res.outcome is None


def test_the_forward_relay_keeps_a_finish_frame_note_and_outcome_O08():
    from collimate.testing.forward import Forwarder
    relay = Forwarder(object())
    relay.dispatch({"op": "finish", "success": True, "note": "a\n  b", "outcome": "pass"})
    assert (relay.result["note"], relay.result["outcome"]) == ("a b", "pass")


@pytest.mark.parametrize("frame", [
    {},
    {"note": 7, "outcome": "maybe"},
    {"note": "   ", "outcome": ["pass"]},
])
def test_the_forward_relay_drops_a_missing_or_bad_verdict_O08(frame):
    from collimate.testing.forward import Forwarder
    relay = Forwarder(object())
    relay.dispatch({"op": "finish", "success": False, **frame})
    assert relay.result["success"] is False
    assert relay.result["note"] is None and relay.result["outcome"] is None
