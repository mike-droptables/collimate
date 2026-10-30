"""The connected-client registry and its handshake endpoint.

Two writers feed one server-lifetime record: the ``COLLIMATE_CLIENT`` env
seed the desktop shell spawns us with, and ``POST /api/system/client``.
The precedence between them is the whole point of this test file — a
browser tab on an Electron-spawned server must end up recorded as a
browser, or generators reach for a native guest view that isn't there.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

import client_caps


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    """The registry is module-level and server-lifetime, so a test that
    writes it would leak into the next one. Re-seed from a cleared env
    before and after each test."""
    monkeypatch.delenv("COLLIMATE_CLIENT", raising=False)
    client_caps.seed_from_env()
    yield
    monkeypatch.delenv("COLLIMATE_CLIENT", raising=False)
    client_caps.seed_from_env()


# ---------------------------------------------------------------------------
# Env seed
# ---------------------------------------------------------------------------


def test_default_seed_is_browser_with_nothing_enabled():
    rec = client_caps.seed_from_env()
    assert rec["client"] == "browser"
    assert rec["source"] == "env"
    assert all(v is False for v in rec["capabilities"].values())
    assert client_caps.client_caps()["client"] == "browser"


def test_electron_env_seeds_the_kind_but_not_the_capabilities(monkeypatch):
    """The shell spawns us before it can describe itself, so the seed
    records *which* client to expect and leaves every capability off until
    the handshake lands. Claiming capabilities here would let a generator
    act on a shell that hasn't confirmed it has them."""
    monkeypatch.setenv("COLLIMATE_CLIENT", "electron")
    rec = client_caps.seed_from_env()
    assert rec["client"] == "electron"
    assert rec["source"] == "env"
    assert all(v is False for v in rec["capabilities"].values())
    assert rec["shell_version"] is None and rec["bridge"] is None


def test_unrecognised_env_value_falls_back_to_browser(monkeypatch):
    monkeypatch.setenv("COLLIMATE_CLIENT", "netscape")
    assert client_caps.seed_from_env()["client"] == "browser"
    assert client_caps.client_caps()["client"] == "browser"


# ---------------------------------------------------------------------------
# Capability normalisation
# ---------------------------------------------------------------------------


def test_wire_camelcase_becomes_server_snake_case():
    got = client_caps.normalize_capabilities({"nativeGuests": True, "directLoopback": True})
    assert got["native_guests"] is True
    assert got["direct_loopback"] is True
    assert got["guest_capture"] is False  # omitted → present and off


def test_unknown_capability_names_survive_normalisation():
    """A remote server can be a separate build from the shell that opens it,
    so a capability name this copy doesn't list must not be silently dropped —
    the known names are a floor, not a filter."""
    got = client_caps.normalize_capabilities({"someFutureThing": True})
    assert got["some_future_thing"] is True
    assert set(client_caps.CAPABILITIES) <= set(got)


# ---------------------------------------------------------------------------
# The handshake endpoint
# ---------------------------------------------------------------------------


_ELECTRON_CAPS = {
    "nativeGuests": True,
    "guestCapture": True,
    "persistentPartitions": True,
    "directLoopback": True,
    "externalOpen": True,
}


def test_handshake_records_the_client(client):
    r = client.post("/api/system/client", json={
        "client": "electron", "shellVersion": "0.4.1", "bridge": 2,
        "capabilities": _ELECTRON_CAPS,
    })
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["client"] == "electron"
    assert rec["shell_version"] == "0.4.1"
    assert rec["bridge"] == 2
    assert rec["source"] == "handshake"
    assert rec["capabilities"]["native_guests"] is True


def test_browser_handshake_overwrites_an_electron_env_seed(client, monkeypatch):
    """The precedence rule. A browser tab pointed at an Electron-spawned
    server is a browser; recording it as anything else would hand its
    generators capabilities the page cannot honour."""
    monkeypatch.setenv("COLLIMATE_CLIENT", "electron")
    client_caps.seed_from_env()
    assert client_caps.client_caps()["client"] == "electron"

    r = client.post("/api/system/client", json={"client": "browser", "capabilities": {}})
    assert r.status_code == 200, r.text
    assert client_caps.client_caps() == {
        "client": "browser", "native_guests": False, "guest_capture": False,
        "persistent_partitions": False, "direct_loopback": False, "external_open": False,
        "custom_title_bar": False, "custom_window_controls": False,
    }


def test_last_handshake_wins(client):
    client.post("/api/system/client", json={
        "client": "electron", "capabilities": _ELECTRON_CAPS,
    })
    r = client.post("/api/system/client", json={"client": "browser", "capabilities": {}})
    assert r.json()["client"] == "browser"
    assert client_caps.client_caps()["client"] == "browser"


def test_unknown_client_kind_is_rejected(client):
    r = client.post("/api/system/client", json={"client": "curl", "capabilities": {}})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# The generator-facing view (what the client_caps IPC op acks)
# ---------------------------------------------------------------------------


def test_generator_view_is_flat_and_always_complete(client):
    client.post("/api/system/client", json={
        "client": "electron", "capabilities": {"nativeGuests": True},
    })
    caps = client_caps.client_caps()
    assert caps["client"] == "electron"
    assert caps["native_guests"] is True
    # Every key present even though the handshake named one — a generator
    # reads caps["external_open"] without a .get guard.
    for name in client_caps.CAPABILITIES:
        assert name in caps


def test_capability_named_client_cannot_shadow_the_client_kind(client):
    """Unknown capability names survive normalisation, but "client" is the
    reserved kind key of the flat view and must win over a capability that
    happens to snake-case to the same name."""
    r = client.post("/api/system/client", json={
        "client": "browser", "capabilities": {"client": True},
    })
    assert r.status_code == 200, r.text
    assert client_caps.client_caps()["client"] == "browser"


def test_ipc_op_is_registered_and_acks_the_flat_view():
    """The op backing gen.client_caps. Registered ungated (absent from
    OP_CAPABILITY) — asking what the UI can do reveals no workspace state."""
    import asyncio

    from process_manager.ipc import OP_CAPABILITY, IPCMixin

    class _Mgr(IPCMixin):
        def __init__(self):
            self.channel = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:client-caps:channel"))
            self.sent: list = []

        async def _send_ack(self, body, _request):
            self.sent.append(body)

    assert "client_caps" in _Mgr()._ipc_handlers
    assert "client_caps" not in OP_CAPABILITY

    mgr = _Mgr()
    asyncio.run(mgr._op_client_caps({"req_id": "test-request", "op": "client_caps"}))
    assert mgr.sent[0]["op"] == "ack_client_caps"
    assert mgr.sent[0]["caps"] == client_caps.client_caps()


# ---------------------------------------------------------------------------
# Cross-copy parity: the capability names are spelled in the desktop shell,
# the page's copy of its interface, this module and the SDK's offline shape
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_capabilities_match_sdk_dev_caps():
    from collimate._host.ipc import DEV_CLIENT_CAPS

    assert set(client_caps.CAPABILITIES) == set(DEV_CLIENT_CAPS) - {"client"}


@pytest.mark.parametrize("rel", [
    "desktop/src/shared/bridge.ts",
    "frontend/src/lib/desktop/index.ts",
])
def test_capabilities_match_desktop_and_frontend_interfaces(rel):
    text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
    start = text.index("export interface DesktopCapabilities {")
    end = text.index("\n}", start)
    block = text[start:end]
    keys = re.findall(r"^\s*(\w+)\s*:\s*boolean\s*;", block, re.M)
    assert keys, f"no capability keys parsed from {rel}"
    assert {client_caps._snake(k) for k in keys} == set(client_caps.CAPABILITIES)
