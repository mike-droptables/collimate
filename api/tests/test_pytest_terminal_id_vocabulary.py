"""Terminal registry and IPC use the PTY identifier's name."""

from sessions import terminal as terminal_sessions

import asyncio
import inspect
import json

from routes import terminal


def test_terminal_registry_pointer_and_websocket_use_terminal_id__F0287():
    pointer = terminal_sessions.register_session("vocab-run", "vocab-cell", "container", ["bash"])
    try:
        assert set(pointer) == {"terminal_id", "ws_path", "token"}
        terminal_id = pointer["terminal_id"]
        assert terminal_id.startswith("tmnl_")
        assert pointer["ws_path"] == f"/api/terminals/{terminal_id}"
        session = terminal_sessions._sessions[terminal_id]
        assert session.terminal_id == terminal_id
        route = next(route for route in terminal.router.routes if route.endpoint is terminal.terminal_ws)
        assert route.path == "/api/terminals/{terminal_id}"
        assert "terminal_id" in inspect.signature(terminal.terminal_ws).parameters
        assert "terminal_id" in inspect.signature(terminal_sessions.session_status).parameters
        assert "terminal_id" in inspect.signature(terminal_sessions.unregister_session).parameters
    finally:
        terminal_sessions.unregister_sessions_for_run("vocab-run")


def test_terminal_ipc_reads_and_destroys_by_terminal_id__F0287(tmp_path):
    from test_pytest_environments import run_with_environment

    run, container = run_with_environment(tmp_path)

    async def dispatch(op, **fields):
        await run._handle_ipc(json.dumps({"op": op, "req_id": "vocab-request", **fields}))

    async def drive():
        await dispatch("create_terminal_session", container_id=container.id, command=["bash"])
        reply = run._ipc_writer.frames[-1]
        assert reply["ok"] is True
        assert set(reply) == {"op", "ok", "req_id", "terminal_id", "ws_path", "token"}
        terminal_id = reply["ws_path"].rsplit("/", 1)[-1]
        # Derive the value from the URL so each request-field check also fails
        # independently if its consumer still reads the former key.
        await dispatch("terminal_status", terminal_id=terminal_id)
        assert run._ipc_writer.frames[-1]["exists"] is True
        await dispatch("destroy_terminal_session", terminal_id=terminal_id)
        assert terminal_id not in terminal_sessions._sessions
        await dispatch("terminal_status", terminal_id=terminal_id)
        assert run._ipc_writer.frames[-1]["exists"] is False

    try:
        asyncio.run(drive())
    finally:
        terminal_sessions.unregister_sessions_for_run(run.run_id)
