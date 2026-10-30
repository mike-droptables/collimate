"""Terminal identities are named independently of chat and harness sessions."""

import json
from dataclasses import fields

from collimate._host.bridge import HostBridge
from collimate.pointers import TERMINAL_KEYS, terminal_pointer
from collimate.testing import Endpoint, run_generator_callable


def test_terminal_bridge_wire_names_the_pty__F0287(monkeypatch):
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    sent = []
    replies = iter([
        {"ok": True, "terminal_id": "tmnl_1", "ws_path": "/ws/1", "token": "t"},
        {"ok": True, "exists": True, "ended": False, "exit_code": None},
    ])
    monkeypatch.setattr(bridge, "_host_available", lambda: True)

    def request(frame, **kwargs):
        sent.append(frame)
        return next(replies)

    monkeypatch.setattr(bridge, "emit_and_wait", request)
    monkeypatch.setattr(bridge, "emit", sent.append)
    terminal = bridge.start_terminal(container_id="c", command=["bash"])
    assert terminal == {"terminal_id": "tmnl_1", "ws_path": "/ws/1", "token": "t"}
    assert bridge.terminal_status(terminal_id="tmnl_1") == {
        "exists": True, "ended": False, "exit_code": None,
    }
    bridge.stop_terminal(terminal_id="tmnl_1")
    assert sent[1:] == [
        {"op": "terminal_status", "terminal_id": "tmnl_1"},
        {"op": "destroy_terminal_session", "terminal_id": "tmnl_1"},
    ]
    pointer = terminal_pointer(terminal, label="Shell")
    assert set(pointer) == set(TERMINAL_KEYS) == {"terminal_id", "ws_path", "token", "label"}
    assert pointer["terminal_id"] == "tmnl_1"


def test_recorded_terminal_pointer_and_endpoint_use_pty_id__F0287():
    async def run():
        import gen

        async with gen.serve_terminal("c", ["bash"], pointer=gen.run_path("shell.terminal")) as tty:
            assert set(tty) == {"terminal_id", "ws_path", "token"}
            gen.update_file("pointer.json", gen.read_file(gen.run_path("shell.terminal")))
            assert gen.terminal_status(terminal_id=tty["terminal_id"])["exists"]

    result = run_generator_callable(run)
    assert result.ok, result.error
    assert json.loads(result.files["pointer.json"])["terminal_id"] == "rec-term-1"
    assert {field.name for field in fields(Endpoint)} == {
        "kind", "name", "url", "port", "path", "terminal_id",
    }
    assert result.endpoints[0].terminal_id == "rec-term-1"
    assert [call for call in result.tier_b_calls if call["op"] == "stop_terminal"] == [
        {"op": "stop_terminal", "terminal_id": "rec-term-1"},
    ]
