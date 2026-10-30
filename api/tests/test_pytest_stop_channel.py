"""A run's channel is part of its control address."""
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.runs.control as control


def test_wrong_channel_stop_does_not_touch_the_run__F0374(monkeypatch):
    calls = []
    own_channel, other_channel = str(uuid4()), str(uuid4())
    manager = SimpleNamespace(channel=own_channel, process=None, stop_requested=False,
                              abort_spawn=lambda: calls.append("abort"),
                              _broadcast_run_status=lambda status: calls.append(status))
    monkeypatch.setattr(control, "active_managers", {"run1": manager})
    monkeypatch.setattr("state.require_channel", lambda channel: None)
    monkeypatch.setattr(control, "mark_run_stopped", lambda *args: calls.append("write"))
    app = FastAPI()
    app.include_router(control.router)
    with TestClient(app) as client:
        assert client.delete(f"/api/channels/{other_channel}/runs/run1").status_code == 404
    assert calls == []
    assert not manager.stop_requested
