"""Coverage for HostBridge.emit_and_wait — IPC ack-await semantics.

The bridge's contract: a missing/dead host raises a typed exception
(``IPCNakError`` / ``IPCTimeoutError`` / ``IPCDisconnectedError``) so
callers can react. Pre-rework, a missing host silently returned ``{}``
and generators blocked forever inside ``readline()``.

These tests fake the socket layer rather than spinning up a real TCP
server — we're verifying the bridge's ack-await state machine, not
network plumbing.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager

import pytest


@contextmanager
def _bridge_with_fake_socket(server_lines=None, hold_open=False):
    """Construct a HostBridge wired to a real localhost TCP socket
    whose server side is driven by ``server_lines``. The yielded
    bridge has its socket attrs populated like a real run.

    ``hold_open=True`` keeps the server side open after sending the
    canned lines so a wait that doesn't get a matching ack times out
    instead of disconnecting.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    accepted: dict = {}

    def _serve():
        client_sock, _ = srv.accept()
        accepted["sock"] = client_sock
        client_file = client_sock.makefile("w", encoding="utf-8")
        request_id = None
        if server_lines:
            incoming = client_sock.makefile("r", encoding="utf-8")
            request_id = json.loads(incoming.readline()).get("req_id")
        for line in (server_lines or []):
            # A test may put multiple replies into one TCP packet.
            replies = []
            for reply_line in line.splitlines():
                try:
                    reply = json.loads(reply_line)
                except json.JSONDecodeError:
                    replies.append(reply_line)
                else:
                    reply.setdefault("req_id", request_id)
                    replies.append(json.dumps(reply))
            client_file.write("\n".join(replies) + "\n")
            client_file.flush()
        if not hold_open:
            client_sock.shutdown(socket.SHUT_WR)
        accepted["file"] = client_file

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    # Build a minimal HostBridge via __new__ so we don't run __init__
    # (signal handling, env-var workspace setup, prompt reads).
    from collimate._host.bridge import HostBridge
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    bridge._socket = socket.create_connection(("127.0.0.1", port))
    bridge._sock_rfile = bridge._socket.makefile("r", encoding="utf-8")
    bridge._sock_wfile = bridge._socket.makefile("w", encoding="utf-8")

    import logging
    bridge.log = logging.getLogger("test")

    for _ in range(100):
        if "sock" in accepted:
            break
        time.sleep(0.01)
    try:
        yield bridge
    finally:
        for f in (bridge._sock_rfile, bridge._sock_wfile, bridge._socket):
            try:
                f.close()
            except Exception:
                pass
        try:
            if "sock" in accepted:
                accepted["sock"].close()
        except Exception:
            pass
        srv.close()
        t.join(timeout=1.0)


def test_emit_and_wait_returns_ack_dict_on_match():
    ack = {"op": "ack_update", "cell_id": "abc"}
    with _bridge_with_fake_socket(server_lines=[json.dumps(ack)]) as bridge:
        out = bridge.emit_and_wait({"op": "update_cell"}, wait_op="ack_update")
    assert out == {**ack, "req_id": out["req_id"]}


def test_emit_and_wait_skips_unrelated_acks_then_returns_match():
    """Stale acks from a previous op (different ``op`` value) are
    skipped silently — the ack-await waits for *its* matching ack."""
    lines = [
        json.dumps({"op": "ack_client_caps", "caps": {}}),
        json.dumps({"op": "ack_update", "cell_id": "matched"}),
    ]
    with _bridge_with_fake_socket(server_lines=lines) as bridge:
        out = bridge.emit_and_wait({"op": "update_cell"}, wait_op="ack_update")
    assert out["cell_id"] == "matched"


def test_emit_and_wait_raises_nak():
    nak = {"op": "nak", "for_op": "update_cell", "reason": "oversize"}
    with _bridge_with_fake_socket(server_lines=[json.dumps(nak)]) as bridge:
        from collimate._host.bridge import IPCNakError
        with pytest.raises(IPCNakError) as ei:
            bridge.emit_and_wait({"op": "update_cell"}, wait_op="ack_update")
    assert ei.value.reason == "oversize"
    assert ei.value.for_op == "update_cell"


def test_emit_and_wait_raises_disconnect_on_eof():
    """Host closes the socket before sending an ack — bridge must
    raise ``IPCDisconnectedError`` rather than block or silently
    return ``{}``."""
    with _bridge_with_fake_socket(server_lines=[]) as bridge:
        from collimate._host.bridge import IPCDisconnectedError
        with pytest.raises(IPCDisconnectedError):
            bridge.emit_and_wait({"op": "update_cell"}, wait_op="ack_update")


def test_emit_and_wait_raises_timeout_when_no_ack_arrives():
    """Server holds the socket open and sends nothing — the bridge
    must surface ``IPCTimeoutError`` after the configured budget."""
    with _bridge_with_fake_socket(server_lines=[], hold_open=True) as bridge:
        from collimate._host.bridge import IPCTimeoutError
        t0 = time.monotonic()
        with pytest.raises(IPCTimeoutError) as ei:
            bridge.emit_and_wait(
                {"op": "update_cell"}, wait_op="ack_update", timeout=0.4,
            )
        elapsed = time.monotonic() - t0
    assert ei.value.op == "ack_update"
    assert ei.value.timeout == 0.4
    # What matters is that the wait honours the budget and is BOUNDED, not
    # how tight the scheduler is: a tight ceiling (it was < 1.0) failed on a
    # machine loaded by parallel suites. A never-returning wait would still
    # hang far past this bound.
    assert 0.4 <= elapsed + 0.05 and elapsed < 0.4 + 5.0


def test_emit_and_wait_dev_mode_returns_empty():
    """Without a connected socket (dev mode — a generator run outside
    the platform), ``emit_and_wait`` returns ``{}`` so standalone
    iteration on generator code keeps working."""
    from collimate._host.bridge import HostBridge
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    bridge._socket = None
    bridge._sock_rfile = None
    bridge._sock_wfile = None
    import logging
    bridge.log = logging.getLogger("test")
    out = bridge.emit_and_wait({"op": "x"}, wait_op="ack_x")
    assert out == {}


def test_emit_and_wait_skips_garbled_lines_and_finds_real_ack():
    """A line that isn't valid JSON (truncated / non-JSON noise) is
    skipped, not treated as fatal — the ack-await keeps reading."""
    lines = [
        "this is not json",
        json.dumps({"op": "ack_update", "cell_id": "ok"}),
    ]
    with _bridge_with_fake_socket(server_lines=lines) as bridge:
        out = bridge.emit_and_wait({"op": "update_cell"}, wait_op="ack_update")
    assert out["cell_id"] == "ok"


@pytest.mark.parametrize("nak", [
    {"op": "nak", "for_op": "port_bound", "reason": "port_not_exposed: 1"},
    {"op": "nak", "for_op": "run_phase", "reason": "permission_denied"},
])
def test_emit_and_wait_ignores_a_nak_for_a_one_way_op(nak):
    """A NAK for a fire-and-forget op (nobody awaits it) is not the answer
    to the call in flight: the call still gets its own ack."""
    ack = {"op": "ack_update", "cell_id": "abc"}
    lines = [json.dumps(nak), json.dumps(ack)]
    with _bridge_with_fake_socket(server_lines=lines, hold_open=True) as bridge:
        out = bridge.emit_and_wait({"op": "update_cell"}, wait_op="ack_update", timeout=2.0)
    assert out == {**ack, "req_id": out["req_id"]}


def test_emit_and_wait_reads_a_second_line_from_the_same_packet__F1185():
    """The NAK and the ack arrive in one write: the ack is already buffered
    and must be returned without waiting for more bytes on the socket."""
    nak = {"op": "nak", "for_op": "port_bound", "reason": "port_not_exposed: 1"}
    ack = {"op": "ack_update", "cell_id": "abc"}
    both = json.dumps(nak) + "\n" + json.dumps(ack)
    with _bridge_with_fake_socket(server_lines=[both], hold_open=True) as bridge:
        t0 = time.monotonic()
        out = bridge.emit_and_wait({"op": "update_cell"}, wait_op="ack_update", timeout=2.0)
        elapsed = time.monotonic() - t0
    assert out == {**ack, "req_id": out["req_id"]}
    assert elapsed < 1.5


def test_finish_twice_then_emit_is_a_no_op():
    import os
    with _bridge_with_fake_socket(server_lines=[], hold_open=True) as bridge:
        bridge._stop_pipe_r, bridge._stop_pipe_w = os.pipe()
        bridge.log_paths = set()
        bridge.finish(success=True)
        bridge.finish(success=True)
        bridge.emit({"op": "phase"})
        assert bridge._sock_wfile is None
        assert bridge.cancelled is True


# ---------------------------------------------------------------------------
# The host's frame cap is checked before a frame is written (F1194, F1250)
# ---------------------------------------------------------------------------


class _RecordingWriter:
    """Wraps the bridge's socket writer, recording every write's length. A
    write past the host's line cap fails the test at once: the fake host
    reads nothing, so forwarded, a line that long would block the test
    forever instead of failing it."""

    def __init__(self, inner):
        self._inner = inner
        self.sizes: list = []
        self.frames: list = []

    def write(self, text):
        from collimate._host.ipc import IPC_MAX_LINE_BYTES
        self.sizes.append(len(text))
        if len(text) > IPC_MAX_LINE_BYTES:
            raise AssertionError(f"a {len(text)}-byte frame reached the host")
        self.frames.append(json.loads(text))
        return self._inner.write(text)

    def flush(self):
        return self._inner.flush()

    def close(self):
        return self._inner.close()


def test_an_oversize_event_raises_and_the_connection_survives__F1194_F1250():
    """F1194 / F1250: an inline event body past the host's 10 MB line cap
    used to reach the host, which closes the connection — every
    later op of the run, ``finish`` included, then failed. The bridge now
    refuses the frame before writing a byte of it, with an error naming the
    op, and the channel keeps working."""
    from collimate._host import bridge as _host
    assert _host.ipc.IPC_MAX_LINE_BYTES == 10_000_000
    ack = {"op": "ack_update", "cell_id": "abc"}
    with _bridge_with_fake_socket(server_lines=[json.dumps(ack)], hold_open=True) as bridge:
        rec = bridge._sock_wfile = _RecordingWriter(bridge._sock_wfile)
        with pytest.raises(ValueError, match="IPC frame limit"):
            bridge.emit({"op": "stream_event", "event": {"text": "x" * 10_000_000}})
        assert rec.sizes == [], "nothing of an oversize frame reaches the host"
        out = bridge.emit_and_wait({"op": "update_cell"}, wait_op="ack_update", timeout=2.0)
    assert out == {**ack, "req_id": out["req_id"]}


def test_the_frame_cap_counts_the_whole_line__F1194(monkeypatch):
    """The cap is the host's: a line of exactly the cap (its newline
    included) is sent, one byte more is refused naming its op."""
    from collimate._host import bridge as _host
    monkeypatch.setattr(_host.ipc, "IPC_MAX_LINE_BYTES", 4096)
    base = len(json.dumps({"op": "fits", "pad": ""})) + 1
    with _bridge_with_fake_socket(server_lines=[], hold_open=True) as bridge:
        rec = bridge._sock_wfile = _RecordingWriter(bridge._sock_wfile)
        bridge.emit({"op": "fits", "pad": "a" * (4096 - base)})
        assert rec.sizes == [4096]
        with pytest.raises(ValueError, match="'too_big' is 4097 bytes"):
            # "too_big" is three characters longer than "fits".
            bridge.emit({"op": "too_big", "pad": "a" * (4096 - base - 2)})
        assert rec.sizes == [4096]


def test_an_oversize_frame_is_refused_without_a_host_too__F1194(monkeypatch):
    """A generator exercised offline fails the same way it would hosted."""
    import logging
    from collimate._host import bridge as _host
    monkeypatch.setattr(_host.ipc, "IPC_MAX_LINE_BYTES", 256)
    bridge = _host.HostBridge.__new__(_host.HostBridge)
    bridge._initialize_ipc()
    bridge._socket = bridge._sock_rfile = bridge._sock_wfile = None
    bridge.log = logging.getLogger("test")
    with pytest.raises(ValueError, match="'stream_event'"):
        bridge.emit({"op": "stream_event", "event": {"text": "x" * 300}})
    bridge.emit({"op": "stream_event", "event": {"text": "x"}})


def test_finish_truncates_a_huge_error_message__F1194(monkeypatch):
    """A failure message longer than any frame should carry is cut to its
    head, so the finish still reaches the host."""
    import os
    with _bridge_with_fake_socket(server_lines=[], hold_open=True) as bridge:
        bridge._stop_pipe_r, bridge._stop_pipe_w = os.pipe()
        bridge.log_paths = set()
        sent: list = []
        bridge.emit = sent.append
        bridge.finish(success=False, error_message="E" * 20_000_000)
    [frame] = sent
    assert frame["success"] is False
    assert frame["error_message"].startswith("E" * 1000)
    assert frame["error_message"].endswith("(truncated)")
    assert len(frame["error_message"]) < 70_000


def _memory_bridge():
    import io
    import logging
    from collimate._host.bridge import HostBridge
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    bridge._socket = None
    bridge._sock_rfile = io.StringIO()
    bridge._sock_wfile = io.StringIO()
    bridge.log = logging.getLogger('test')
    return bridge


def test_one_way_write_does_not_wait_for_a_request__F1186_F1246_F1178():
    bridge = _memory_bridge()
    reading, release, sent = threading.Event(), threading.Event(), threading.Event()
    def read(*_):
        req_id = _last_request(bridge)['req_id']
        reading.set()
        assert release.wait(3)
        return json.dumps({"op": "ack_slow", "req_id": req_id}) + "\n"
    bridge._read_line = read
    request = threading.Thread(target=lambda: bridge.emit_and_wait({'op': 'slow'}, 'ack_slow'))
    writer = threading.Thread(target=lambda: (bridge.emit({'op': 'status'}), sent.set()))
    request.start()
    assert reading.wait(3)
    writer.start()
    try:
        assert sent.wait(0.3), 'fire-and-forget write blocked behind an ack wait'
    finally:
        release.set()
        request.join(3)
        writer.join(3)
        assert not request.is_alive() and not writer.is_alive()


def test_queued_request_gets_its_full_deadline__F1124_F1143(monkeypatch):
    from types import SimpleNamespace
    from collimate._host import bridge as _host
    bridge = _memory_bridge()
    first_read, second_framed, release = threading.Event(), threading.Event(), threading.Event()
    clock = [0.0]
    deadlines = []
    monkeypatch.setattr(_host, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    frame = _host._frame
    def framed(payload):
        if payload['op'] == 'second':
            second_framed.set()
        return frame(payload)
    monkeypatch.setattr(_host, '_frame', framed)
    def read(deadline, op, budget):
        if op == 'ack_first':
            first_read.set()
            assert release.wait(3)
        else:
            deadlines.append((deadline, clock[0], budget))
        return _memory_reply(bridge, {'op': op})
    bridge._read_line = read
    first = threading.Thread(target=lambda: bridge.emit_and_wait({'op': 'first'}, 'ack_first', timeout=10))
    second = threading.Thread(target=lambda: bridge.emit_and_wait({'op': 'second'}, 'ack_second', timeout=10))
    first.start()
    assert first_read.wait(3)
    second.start()
    try:
        assert second_framed.wait(3)
        clock[0] = 100.0
    finally:
        release.set()
        first.join(3)
        second.join(3)
    assert deadlines == [(110.0, 100.0, 10.0)]


def test_timed_out_bridge_refuses_a_late_same_op_answer__F1187():
    from collimate._host.bridge import IPCTimeoutError, IPCDisconnectedError
    bridge = _memory_bridge()
    def timeout(*_):
        raise IPCTimeoutError('ack_update', 0.1)
    bridge._read_line = timeout
    with pytest.raises(IPCTimeoutError):
        bridge.emit_and_wait({'op': 'update_cell'}, 'ack_update', timeout=0.1)
    sent = bridge._sock_wfile.getvalue()
    bridge._read_line = lambda *_: '{"op":"ack_update","cell_id":"stale"}\n'
    with pytest.raises(IPCDisconnectedError, match='desynchron'):
        bridge.emit_and_wait({'op': 'update_cell'}, 'ack_update')
    assert bridge._sock_wfile.getvalue() == sent


@pytest.mark.parametrize('for_op', [None, 'unknown', 'status'])
def test_nak_must_name_the_current_operation__F0090(for_op):
    bridge = _memory_bridge()
    frames = iter([json.dumps({'op': 'nak', 'for_op': for_op, 'reason': 'handler_error'}),
                   '{"op":"ack_update","cell_id":"current"}'])
    bridge._read_line = lambda *_: _memory_reply(bridge, json.loads(next(frames)))
    assert bridge.emit_and_wait({'op': 'update_cell'}, 'ack_update')['cell_id'] == 'current'


def test_port_refusal_raises_at_serve_port_and_next_update_survives__F2028():
    from collimate._host.bridge import IPCNakError
    bridge = _memory_bridge()
    replies = iter(['{"op":"nak","for_op":"port_bound","reason":"port_not_exposed: 9000"}\n',
                    '{"op":"ack_update","ok":true,"cell_id":"next"}\n'])
    bridge._read_line = lambda *_: _memory_reply(bridge, json.loads(next(replies)))
    with pytest.raises(IPCNakError, match='port_not_exposed'):
        bridge.serve_port(9000)
    assert bridge.emit_and_wait({'op': 'update_cell'}, 'ack_update')['cell_id'] == 'next'


def _last_request(bridge):
    return json.loads(bridge._sock_wfile.getvalue().splitlines()[-1])


def _memory_reply(bridge, reply):
    return json.dumps({**reply, 'req_id': _last_request(bridge).get('req_id')}) + '\n'


def test_each_request_has_a_fresh_id_without_mutating_its_payload__F0153():
    bridge = _memory_bridge()
    ids = []
    def read(*_):
        req_id = _last_request(bridge).get('req_id')
        assert isinstance(req_id, str) and req_id
        ids.append(req_id)
        return json.dumps({'op': 'ack_update', 'req_id': req_id}) + '\n'
    bridge._read_line = read
    payload = {'op': 'update_cell'}
    bridge.emit_and_wait(payload, 'ack_update')
    bridge.emit_and_wait(payload, 'ack_update')
    assert len(set(ids)) == 2
    assert payload == {'op': 'update_cell'}


def test_replies_require_exact_request_ids_without_op_fallback__F0153():
    bridge = _memory_bridge()
    replies = iter([
        {'op': 'ack_update', 'req_id': 'stale-request', 'cell_id': 'wrong'},
        {'op': 'ack_update', 'cell_id': 'missing'},
        {'op': 'nak', 'for_op': 'update_cell', 'req_id': 'another-request', 'reason': 'wrong'},
        {'op': 'ack_update', 'cell_id': 'current'},
    ])
    def read(*_):
        reply = next(replies)
        if reply.get('cell_id') == 'current':
            reply['req_id'] = _last_request(bridge).get('req_id')
        return json.dumps(reply) + '\n'
    bridge._read_line = read
    assert bridge.emit_and_wait({'op': 'update_cell'}, 'ack_update')['cell_id'] == 'current'


def test_the_ipc_frame_cap_has_one_public_owner__P0F01():
    from collimate._host.ipc import IPC_MAX_LINE_BYTES
    assert IPC_MAX_LINE_BYTES == 10_000_000


def test_env_exec_does_not_send_an_unenforced_command_timeout__F1195():
    bridge = _memory_bridge()
    requests = []
    bridge.emit_and_wait = lambda payload, **kw: requests.append((payload, kw)) or {}
    bridge.env_exec('env-one', ['sleep', '20'], timeout=10)
    payload, options = requests[0]
    assert 'timeout' not in payload
    assert options['timeout'] == 40


def test_partial_request_write_marks_the_channel_desynchronized__F1187():
    from collimate._host.bridge import IPCDisconnectedError
    bridge = _memory_bridge()
    def partial_send(_frame):
        raise OSError('partial socket write')
    bridge._send = partial_send
    with pytest.raises(OSError, match='partial socket write'):
        bridge.emit_and_wait({'op': 'update_cell'}, 'ack_update')
    sent = []
    bridge._send = sent.append
    bridge._read_line = lambda *_: (_ for _ in ()).throw(AssertionError('must not read'))
    with pytest.raises(IPCDisconnectedError, match='desynchron'):
        bridge.emit_and_wait({'op': 'update_cell'}, 'ack_update')
    assert sent == []
