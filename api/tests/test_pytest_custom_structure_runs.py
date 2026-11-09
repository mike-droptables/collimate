"""A custom account role by exact structure, through a runner and a run.

The generator declares the structure it reads; the user's entry with that
structure is bound in the runner; the run receives the entry's detached
snapshot (public values nested, secrets by path) and its history keeps the
public part only. An entry whose structure changes stops filling the role:
the runner is invalid and a start is refused.
"""
from __future__ import annotations

import json

import yaml

from test_pytest_accounts import registry as registry, TRACKER, tracker
from test_pytest_runners import runner_app as runner_app, draft
from run_spawn import wait_spawned


def _setup(runner_app):
    import collimate_server.accounts as accounts
    client, _channel, source, _target = runner_app
    entry = accounts.create_account(tracker())
    manifest = next(f for f in source.files if f.name.endswith("choice.generator"))
    metadata = yaml.safe_load(manifest.content)
    metadata["account_requirements"] = {"tracker": {"label": "Issue tracker", "kind": "custom", "structure": TRACKER}}
    manifest.content = yaml.safe_dump(metadata)
    body = draft()
    body["runner"]["accounts"] = {"tracker": entry["id"]}
    saved = client.post("/api/runners", json=body)
    assert saved.status_code == 200, saved.text
    return entry


def test_the_run_gets_the_snapshot_and_history_keeps_the_public_part(runner_app, registry, monkeypatch):
    import collimate_server.routes.runs.create as create
    import collimate_server.run_registry as run_registry
    from collimate_server.database import get_db_connection
    client, _, _, target = runner_app
    _setup(runner_app)
    listed = client.get("/api/runner_generators").json()[0]["account_requirements"]["tracker"]
    assert listed["structure"] == TRACKER
    managers = []

    class Manager:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            managers.append(self)

        async def start(self):
            pass

    async def reap(*args):
        pass
    monkeypatch.setattr(create, "ProcessManager", Manager)
    monkeypatch.setattr(create, "_reap_process", reap)
    monkeypatch.setattr(create, "active_managers", {})
    monkeypatch.setattr(run_registry, "active_managers", {})
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": "example", "cell_id": str(target.id)})
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    wait_spawned(run_id)
    snapshot = managers[0].account_context["tracker"]
    assert snapshot["metadata"]["values"] == {"url": "https://issues.example", "auth": {"user": "ada"}}
    assert snapshot["secrets"] == {"auth.token": "tracker-private"}
    conn = get_db_connection(client.channel_id)
    try:
        row = conn.execute("SELECT runner_snapshot FROM runs WHERE id=?", (run_id,)).fetchone()
    finally:
        conn.close()
    assert "tracker-private" not in row["runner_snapshot"]
    history = json.loads(row["runner_snapshot"])["accounts"]["tracker"]
    assert history["metadata"] == {"structure": TRACKER, "values": {"url": "https://issues.example", "auth": {"user": "ada"}}}
    assert "secrets" not in history and "secret_fields" not in history


def test_an_entry_whose_structure_changed_makes_the_runner_invalid(runner_app, registry):
    import collimate_server.accounts as accounts
    client, _, _, target = runner_app
    entry = _setup(runner_app)
    assert "invalid" not in next(r for r in client.get("/api/runners").json() if r["id"] == "example")
    accounts.update_account(entry["id"], {"metadata": {"structure": {**TRACKER, "region": "public"},
                                                       "values": {"url": "https://issues.example"}}})
    listed = next(r for r in client.get("/api/runners").json() if r["id"] == "example")
    assert "does not have the structure" in listed["invalid"]["reason"]
    refused = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": "example", "cell_id": str(target.id)})
    assert refused.status_code == 400 and "does not have the structure" in refused.text
