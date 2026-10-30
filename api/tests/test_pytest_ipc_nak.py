"""IPC replies, protocol failures and per-message failure isolation."""
from __future__ import annotations

import asyncio
import json
from collections import deque
from unittest.mock import AsyncMock, MagicMock


class _FakeWriter:
    """Stand-in for asyncio.StreamWriter that captures every line written."""

    def __init__(self):
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        return None


def _make_inst(writer):
    """Bare IPCMixin instance with just enough state for NAK + decode-
    error paths (no DB, no broadcast). _send_nak / _handle_ipc resolve
    via inheritance, so the mixin's own methods can call each other."""
    from process_manager.ipc import IPCMixin

    class _Stub(IPCMixin):
        pass

    inst = _Stub()
    inst._ipc_writer = writer
    inst.log_buffer = deque()
    return inst


def _send_nak(writer, for_op, reason):
    inst = _make_inst(writer)
    asyncio.run(inst._send_nak(for_op, reason, req_id="test-request"))
    return inst


def _handle_ipc(writer, line):
    inst = _make_inst(writer)
    asyncio.run(inst._handle_ipc(line))
    return inst


def test_send_nak_writes_well_formed_frame():
    w = _FakeWriter()
    _send_nak(w, "update_cell", "boom")
    assert len(w.lines) == 1
    decoded = json.loads(w.lines[0].decode().strip())
    assert decoded == {"op": "nak", "for_op": "update_cell", "reason": "boom", "req_id": "test-request"}


def test_live_file_reply_preserves_type_revision_and_exact_bytes__F1228_F0189_F0190(fresh_cwd):
    import base64
    from cas import store_bytes
    writer = _FakeWriter()
    inst = _make_inst(writer)
    bodies = {"text.txt": "Résumé".encode(), "binary.bin": b"\xff\x00", "empty.bin": b""}
    metadata = {path: {"body_hash": store_bytes(raw), "file_revision": index + 1,
                       "encoding": "utf-8" if path.endswith(".txt") else "binary"}
                for index, (path, raw) in enumerate(bodies.items())}
    observed = []

    def current(paths, revisions):
        observed.append((paths, revisions))
        return metadata

    inst._cell_live_metadata = current
    asyncio.run(inst._op_read_cell_files({"req_id": "read-files", "paths": list(bodies),
                                         "revisions": {path: value["file_revision"] for path, value in metadata.items()}}))
    reply = json.loads(writer.lines[0])
    assert reply["ok"] is True
    assert reply["req_id"] == "read-files"
    assert observed == [(set(bodies), {path: value["file_revision"] for path, value in metadata.items()})]
    for path, body in bodies.items():
        assert base64.b64decode(reply["files"][path].pop("content")) == body
        assert reply["files"][path] == metadata[path]


def test_send_nak_silently_skips_when_writer_missing():
    """Channel torn down mid-handshake — _send_nak must not raise."""
    inst = _make_inst(writer=None)
    asyncio.run(inst._send_nak("x", "y", req_id="test-request"))
    # No assertion needed — passing without exception is the contract.


def test_invalid_json_closes_without_unmatched_nak__F0090():
    writer = _FakeWriter()
    inst = _handle_ipc(writer, "not a valid json line {{{")
    assert writer.lines == []
    assert any(e.get("type") == "ipc_error" for e in inst.log_buffer)


def test_handle_ipc_logs_truncate_huge_invalid_payload():
    """A 1 MB blob of garbage shouldn't drown the ipc_error log stream
    with the entire payload."""
    big_garbage = "x" * 1_000_000
    w = _FakeWriter()
    inst = _handle_ipc(w, big_garbage)
    err_log = next(e for e in inst.log_buffer if e.get("type") == "ipc_error")
    assert len(err_log["payload"]) < 500, (
        "ipc_error log should truncate huge invalid payloads"
    )


def test_handle_ipc_connection_closes_on_oversize_line__F0090():
    """An oversize frame cannot be correlated and must close without reply."""
    from process_manager.ipc import IPC_MAX_LINE_BYTES

    # Build a fake reader that yields one oversize line then EOF.
    big_line = (b"x" * (IPC_MAX_LINE_BYTES + 1)) + b"\n"

    async def _scenario():
        reader = MagicMock()
        # readline() returns the oversize line, then b"" to signal EOF.
        reader.readline = AsyncMock(side_effect=[big_line, b""])
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        # Capture .write calls.
        writer.write_lines = []
        writer.drain = AsyncMock()
        def _capture(data):
            writer.write_lines.append(data)
        writer.write = _capture

        # _ipc_writer is reassigned inside _handle_ipc_connection.
        inst = _make_inst(writer=None)
        await inst._handle_ipc_connection(reader, writer)
        return inst, reader, writer

    inst, reader, writer = asyncio.run(_scenario())

    assert writer.write_lines == []

    # And an ipc_error log for operator visibility.
    err_logs = [e for e in inst.log_buffer if e.get("type") == "ipc_error"]
    assert any("oversize" in e["payload"] for e in err_logs)

    # The connection must be dropped — a peer ignoring the
    # frame cap shouldn't get to keep streaming oversized data.
    writer.close.assert_called_once()
    assert reader.readline.await_count == 1


def test_handle_ipc_connection_closes_when_reader_limit_trips__F0090():
    """readline() raises ValueError (wrapping LimitOverrunError) when a
    line exceeds the StreamReader limit; that path must log and close."""

    async def _scenario():
        reader = MagicMock()
        reader.readline = AsyncMock(side_effect=ValueError("Separator is not found"))
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        writer.write_lines = []
        writer.drain = AsyncMock()
        writer.write = lambda data: writer.write_lines.append(data)

        inst = _make_inst(writer=None)
        await inst._handle_ipc_connection(reader, writer)
        return inst, writer

    inst, writer = asyncio.run(_scenario())

    assert writer.write_lines == []
    err_logs = [e for e in inst.log_buffer if e.get("type") == "ipc_error"]
    assert any("oversize" in e["payload"] for e in err_logs)
    writer.close.assert_called_once()


def test_handle_ipc_connection_teardown_survives_peer_reset():
    """An abruptly reset peer makes wait_closed() raise. The teardown must
    still clear this connection's writer, not raise out of the connection
    callback, and leave a newer connection's writer installed."""

    def _conn_writer():
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock(side_effect=ConnectionResetError("reset"))
        return writer

    async def _scenario(replace_with):
        reader = MagicMock()
        reader.readline = AsyncMock(return_value=b"")
        writer = _conn_writer()
        inst = _make_inst(writer=None)
        if replace_with is not None:
            # A second connection installs its writer while this one is open.
            async def _eof():
                inst._ipc_writer = replace_with
                return b""
            reader.readline = AsyncMock(side_effect=_eof)
        await inst._handle_ipc_connection(reader, writer)
        return inst, writer

    inst, writer = asyncio.run(_scenario(None))
    writer.close.assert_called_once()
    assert inst._ipc_writer is None

    other = _FakeWriter()
    inst, _ = asyncio.run(_scenario(other))
    assert inst._ipc_writer is other


def test_handle_ipc_no_nak_for_valid_unknown_op():
    """An unknown op is logged under a fixed type without a stray reply."""
    writer = _FakeWriter()
    inst = _handle_ipc(writer, json.dumps({"op": "removed_op", "x": 1}))
    assert writer.lines == []
    assert [entry["type"] for entry in inst.log_buffer] == ["ipc_error", "ipc_unknown_op"]


def test_request_handler_failure_keeps_next_request_on_connection__F0091():
    async def scenario():
        writer = _FakeWriter()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        inst = _make_inst(writer)
        calls = 0

        async def handler(data):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("failed poll")
            await inst._send_ack({"op": "ack_chat_poll", "command": "next"}, data)

        inst._ipc_handlers_cache = {"chat_poll": handler}
        reader = MagicMock()
        reader.readline = AsyncMock(side_effect=[
            b'{"op":"chat_poll","req_id":"first"}\n', b'{"op":"chat_poll","req_id":"second"}\n', b'',
        ])
        await inst._handle_ipc_connection(reader, writer)
        assert calls == 2
        assert [json.loads(line) for line in writer.lines] == [
            {"op": "nak", "for_op": "chat_poll", "reason": "handler_error", "error": "failed poll", "req_id": "first"},
            {"op": "ack_chat_poll", "command": "next", "req_id": "second"},
        ]

    asyncio.run(scenario())


def test_one_way_handler_or_permission_failure_does_not_nak__F0090(monkeypatch):
    from process_manager import ipc

    async def scenario(denied):
        writer = _FakeWriter()
        inst = _make_inst(writer)
        inst.permissions = MagicMock()
        inst.permissions.allows.return_value = not denied

        async def fail(data):
            raise RuntimeError("notification failed")

        inst._ipc_handlers_cache = {"phase": fail}
        await inst._handle_ipc('{"op":"phase"}')
        assert writer.lines == []
        assert inst.log_buffer

    monkeypatch.setitem(ipc.OP_CAPABILITY, "phase", "test")
    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_port_bound_answers_the_call_that_registered_it__F2028():
    async def scenario():
        writer = _FakeWriter()
        inst = _make_inst(writer)
        inst.serves_port = lambda port: port == 8080
        inst.bound_ports = set()
        inst.log_refusal = MagicMock()
        await inst._handle_ipc('{"op":"port_bound","port":8080,"req_id":"port-1"}')
        await inst._handle_ipc('{"op":"port_bound","port":8081,"req_id":"port-2"}')
        assert inst.bound_ports == {8080}
        assert [json.loads(line) for line in writer.lines] == [
            {"op": "ack_port_bound", "ok": True, "req_id": "port-1"},
            {"op": "nak", "for_op": "port_bound", "reason": "port_not_exposed: 8081", "req_id": "port-2"},
        ]

    asyncio.run(scenario())


def test_success_and_refusal_echo_exact_request_ids__F0153():
    async def scenario():
        writer = _FakeWriter()
        inst = _make_inst(writer)
        inst.serves_port = lambda port: port == 8080
        inst.bound_ports = set()
        inst.log_refusal = MagicMock()
        for req_id, port in [("first:request", 8080), ("second:request", 8081)]:
            await inst._handle_ipc(json.dumps({"op": "port_bound", "port": port, "req_id": req_id}))
        frames = [json.loads(line) for line in writer.lines]
        assert [frame.get("req_id") for frame in frames] == ["first:request", "second:request"]
        assert [frame["op"] for frame in frames] == ["ack_port_bound", "nak"]

    asyncio.run(scenario())


def test_handler_error_and_permission_denial_echo_request_id__F0153():
    async def scenario(allowed):
        writer = _FakeWriter()
        inst = _make_inst(writer)
        inst.permissions = MagicMock()
        inst.permissions.allows.return_value = allowed

        async def fail(data):
            raise RuntimeError("no placement")

        inst._ipc_handlers_cache = {"env_place": fail}
        await inst._handle_ipc('{"op":"env_place","req_id":"place-1"}')
        [reply] = [json.loads(line) for line in writer.lines]
        assert reply.get("req_id") == "place-1"
        assert reply["reason"] == ("handler_error" if allowed else "permission_denied")

    asyncio.run(scenario(True))
    asyncio.run(scenario(False))


def test_request_without_valid_id_never_dispatches__F0153():
    async def scenario(req_id):
        writer = _FakeWriter()
        inst = _make_inst(writer)
        handler = AsyncMock()
        inst._ipc_handlers_cache = {"client_caps": handler}
        request = {"op": "client_caps"}
        if req_id is not None:
            request["req_id"] = req_id
        result = await inst._handle_ipc(json.dumps(request))
        assert result is False
        handler.assert_not_called()
        assert writer.lines == []
        assert "req_id" in inst.log_buffer[-1]["payload"]

    for invalid in [None, "", 42, True, [], {}]:
        asyncio.run(scenario(invalid))
