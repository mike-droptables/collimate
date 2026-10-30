# SPDX-License-Identifier: MIT
"""gen.client_caps() — what the client driving the app can do.

Covers the whole ladder: the IPC round trip over a real socket, the
dev-mode degradation (no host → ``client="dev"``, everything off), and
the two ways a test seeds caps through the harness (``run_generator_callable(client_caps=…)`` and
``bridge.stub_client_caps``).

The invariant every layer shares: the returned dict always carries every
capability key, so a generator writes ``caps["native_guests"]`` and not
``caps.get("native_guests", False)``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time

import pytest
from contextlib import contextmanager

from collimate import native
from collimate._host.bridge import IPCDisconnectedError
from collimate._host.ipc import DEV_CLIENT_CAPS
from collimate.testing import run_generator_callable

# The canonical capability set, mirroring ``api/client_caps.py``
# CAPABILITIES on the app side. Spelled out in full (not derived from
# ``DEV_CLIENT_CAPS``) so a key renamed or dropped on one side of the
# mirror fails here instead of passing vacuously.
_CAP_KEYS = (
    "native_guests", "guest_capture", "persistent_partitions",
    "direct_loopback", "external_open", "custom_title_bar",
    "custom_window_controls",
)


@contextmanager
def _bridge_with_fake_socket(server_lines=None, *, echo_request_id=True):
    """Minimal HostBridge over a real localhost socket whose server side
    replays canned ack lines and records what the bridge emitted."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    accepted: dict = {"received": []}

    def _serve():
        client_sock, _ = srv.accept()
        accepted["sock"] = client_sock
        rfile = client_sock.makefile("r", encoding="utf-8")
        wfile = client_sock.makefile("w", encoding="utf-8")
        accepted["received"].append(rfile.readline())
        request = json.loads(accepted["received"][0])
        for line in (server_lines or []):
            ack = json.loads(line)
            if echo_request_id:
                ack["req_id"] = request["req_id"]
            wfile.write(json.dumps(ack) + "\n")
            wfile.flush()
        client_sock.shutdown(socket.SHUT_WR)

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    from collimate._host.bridge import HostBridge
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    bridge._socket = socket.create_connection(("127.0.0.1", port))
    bridge._sock_rfile = bridge._socket.makefile("r", encoding="utf-8")
    bridge._sock_wfile = bridge._socket.makefile("w", encoding="utf-8")
    bridge.log = logging.getLogger("test")

    for _ in range(100):
        if "sock" in accepted:
            break
        time.sleep(0.01)
    try:
        yield bridge, accepted
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


# ---------------------------------------------------------------------------
# IPC contract
# ---------------------------------------------------------------------------


def test_client_caps_round_trips_over_ipc__F0153():
    caps = {
        "client": "electron", "native_guests": True, "guest_capture": True,
        "persistent_partitions": True, "direct_loopback": True,
        "external_open": True, "custom_title_bar": True,
        "custom_window_controls": True,
    }
    ack = {"op": "ack_client_caps", "caps": caps}
    with _bridge_with_fake_socket(server_lines=[json.dumps(ack)]) as (bridge, accepted):
        got = bridge.client_caps()
    assert got == caps
    request = json.loads(accepted["received"][0])
    assert set(request) == {"op", "req_id"}
    assert request["op"] == "client_caps"
    assert isinstance(request["req_id"], str) and request["req_id"]


def test_unmatched_ack_disconnects_without_dev_fallback__F0153():
    """A reply without the request identity cannot satisfy the capability call."""
    ack = {"op": "ack_client_caps", "caps": "electron"}
    with _bridge_with_fake_socket(
        server_lines=[json.dumps(ack)], echo_request_id=False,
    ) as (bridge, _), pytest.raises(IPCDisconnectedError, match="host closed"):
        bridge.client_caps()


# ---------------------------------------------------------------------------
# Dev mode + the harness
# ---------------------------------------------------------------------------


def test_dev_mode_reports_dev_client_with_everything_off(native_bridge):
    caps = asyncio.run(native.client_caps())
    assert caps["client"] == "dev"
    assert all(caps[k] is False for k in _CAP_KEYS)
    # Exactly the canonical set, no more and no less: a capability added on
    # the app side but never mirrored here (or a stale key left behind after
    # a rename) is drift, and drift is what this pins.
    assert set(caps) == {"client", *_CAP_KEYS}
    assert set(DEV_CLIENT_CAPS) == {"client", *_CAP_KEYS}


def test_harness_kwarg_seeds_caps():
    async def run():
        import gen
        caps = await gen.client_caps()
        gen.update_file("client.txt", caps["client"])
        gen.update_file("guests.txt", str(caps["native_guests"]))
        # Unnamed caps still resolve — no .get guard needed.
        gen.update_file("capture.txt", str(caps["guest_capture"]))

    res = run_generator_callable(
        run, client_caps={"client": "electron", "native_guests": True},
    )
    assert res.ok, res.error
    assert res.files["client.txt"] == "electron"
    assert res.files["guests.txt"] == "True"
    assert res.files["capture.txt"] == "False"


def test_stub_client_caps_setter(native_bridge):
    native_bridge.stub_client_caps({"client": "browser", "external_open": True})
    caps = asyncio.run(native.client_caps())
    assert caps["client"] == "browser"
    assert caps["external_open"] is True
    assert caps["native_guests"] is False
    # Reset restores the dev answer rather than leaving the last stub behind.
    native_bridge.stub_client_caps(None)
    assert asyncio.run(native.client_caps())["client"] == "dev"


# ---------------------------------------------------------------------------
# Surface presence
# ---------------------------------------------------------------------------


def test_client_caps_is_in_the_native_surface():
    assert "client_caps" in native.__all__
    assert hasattr(native, "client_caps")
