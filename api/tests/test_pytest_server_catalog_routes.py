"""The two routes the desktop shell reads from every server it knows (MS-03).

``GET /api/channels`` is the light catalog the shell polls per saved server:
each channel's id and name and the active id, nothing else. ``GET
/api/system/instance`` is the identity the shell checks before adopting a
server it left running: pid, absolute data dir, instance id, and the same
version and bridge ``/health`` reports. Both are guarded like every route;
``/health`` stays the public handshake.
"""
from __future__ import annotations

import hashlib
import os

import pytest
from test_pytest_auth_middleware import TOKEN, authed_client as authed_client
from test_pytest_handlers import client as client

EVIL = "https://evil.example"


def _set_channels(names: list[str], active: int | None):
    from models import Channel
    from state import state
    channels = [Channel(name=n) for n in names]
    state.channels = channels
    state.active_channel_id = channels[active].id if active is not None else None
    return channels


def test_channel_catalog_lists_ids_names_and_the_active_id_MS03(client):
    channels = _set_channels(["Alpha", "Beta", "Gamma"], active=1)
    body = client.get("/api/channels").json()
    assert body == {
        "channels": [{"id": str(c.id), "name": c.name} for c in channels],
        "active_channel_id": str(channels[1].id),
    }


def test_channel_catalog_without_an_active_channel_MS03(client):
    from uuid import uuid4
    from state import state
    channels = _set_channels(["Only"], active=None)
    assert client.get("/api/channels").json() == {
        "channels": [{"id": str(channels[0].id), "name": "Only"}], "active_channel_id": None}
    # An active id naming no registered channel is not an active channel.
    state.active_channel_id = uuid4()
    assert client.get("/api/channels").json()["active_channel_id"] is None
    _set_channels([], active=None)
    assert client.get("/api/channels").json() == {"channels": [], "active_channel_id": None}


def test_instance_names_this_process_and_data_dir_MS03(client, monkeypatch):
    import instance
    monkeypatch.setattr(instance, "_cached_id", None)
    cwd = os.path.abspath(os.getcwd())
    health = client.get("/health").json()
    body = client.get("/api/system/instance").json()
    assert body == {
        "pid": os.getpid(),
        "data_dir": cwd,
        "instance": hashlib.sha1(cwd.encode("utf-8", "surrogateescape")).hexdigest()[:12],
        "version": health["version"],
        "bridge": health["bridge"],
    }
    assert body["instance"] == instance.instance_id()
    assert body["bridge"] == 6


@pytest.mark.parametrize("path", ["/api/system/instance", "/api/channels"])
def test_catalog_and_instance_refuse_a_foreign_origin_MS03(client, path):
    r = client.get(path, headers={"Origin": EVIL})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers
    assert client.get(path, headers={"Origin": "null"}).status_code == 403


def test_health_stays_the_public_handshake_MS03(client):
    # The shell's main-process fetch carries no Origin: /health answers it
    # with the handshake alone, nothing of the instance identity.
    r = client.get("/health")
    assert r.status_code == 200
    assert set(r.json()) == {"status", "version", "bridge"}


@pytest.mark.parametrize("path", ["/api/system/instance", "/api/channels"])
def test_tokened_server_needs_the_bearer_for_catalog_and_instance_MS03(authed_client, path):
    assert authed_client.get(path).status_code == 401
    assert authed_client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert authed_client.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    # /health needs no token: it is how the shell learns it can talk at all.
    assert authed_client.get("/health").status_code == 200
