"""Account-derived main config uses the startup snapshot, including run gates."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from test_pytest_accounts import registry as registry, model
from test_pytest_runners import runner_app as runner_app, draft
from run_spawn import wait_spawned


def _setup(runner_app, harness="codex", start="terminal", property_path="model"):
    import accounts
    client, channel, source, target = runner_app
    account = accounts.create_account(model(endpoint="https://api.openai.com/v1", model="current-model", metadata={"harness": harness}))
    manifest = next(f for f in source.files if f.name.endswith("choice.generator"))
    metadata = yaml.safe_load(manifest.content)
    metadata["account_requirements"] = {"model": {"kind": "model", "capabilities": ["text"]}}
    manifest.content = yaml.safe_dump(metadata)
    schema = next(f for f in source.files if f.name.endswith("settings.set_yaml"))
    schema.content = yaml.safe_dump({"fields": [
        {"key": "backend", "type": "dropdown", "options": ["pydantic-ai", "codex"], "account_role": "model", "account_property": "metadata.harness", "default": "pydantic-ai"},
        {"key": "model", "type": "text", "account_role": "model", "account_property": property_path, "default": ""},
        {"key": "start", "label": "Start mode", "type": "dropdown", "options": [
            {"value": "chat", "label": "Chat"}, {"value": "terminal", "label": "Terminal", "enable_if": {"backend": "codex"}},
        ]},
    ]})
    body = draft()
    body["runner"]["settings"] = {"settings.set_yaml": {"backend": "pydantic-ai", "model": "stale-model", "start": start}}
    body["runner"]["accounts"] = {"model": account["id"]}
    saved = client.post("/api/runners", json=body)
    assert saved.status_code == 200, saved.text
    return account, saved.json()


def test_http_start_stages_snapshot_values_without_rewriting_saved_runner(runner_app, registry, monkeypatch):
    import accounts
    import routes.runs.create as create
    import run_registry as run_registry
    from collimate.config_values import parse_set_yaml, resolve_form_values
    from database import get_db_connection
    client, _, _, target = runner_app
    account, saved = _setup(runner_app)
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
    original_resolve = accounts.resolve_accounts

    def capture_then_edit(*args):
        captured = original_resolve(*args)
        accounts.update_account(account["id"], {"model": "next-run-model", "metadata": {"harness": "pydantic-ai"}})
        return captured

    monkeypatch.setattr(accounts, "resolve_accounts", capture_then_edit)
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": "example", "cell_id": str(target.id)})
    assert response.status_code == 200, response.text
    wait_spawned(response.json()["run_id"])
    manager = managers[0]
    expected = {"backend": "codex", "model": "current-model", "start": "terminal"}
    config = Path(manager.workspace_dir, ".collrun/config/files/settings.set_yaml")
    assert resolve_form_values(parse_set_yaml(config.read_text()))[1] == expected
    conn = get_db_connection(client.channel_id)
    try:
        row = conn.execute("SELECT runner_snapshot FROM runs WHERE id=?", (response.json()["run_id"],)).fetchone()
        assert "private-one" not in row["runner_snapshot"]
        snapshot = json.loads(row["runner_snapshot"])
        assert snapshot["settings"]["settings.set_yaml"] == expected
        assert snapshot["revision"] == saved["revision"]
    finally:
        conn.close()
    assert client.get("/api/runners/example").json()["runner"]["settings"] == saved["runner"]["settings"]
    assert manager.account_context["model"]["model"] == "current-model"


def _placement_setup(runner_app, harness, surface, permissions, *, opts_in=True):
    import accounts
    client, _channel, source, _target = runner_app
    account = accounts.create_account(model(endpoint="https://api.openai.com/v1", model="current-model",
                                            metadata={"harness": harness}))
    manifest = next(f for f in source.files if f.name.endswith("choice.generator"))
    metadata = yaml.safe_load(manifest.content)
    metadata["account_requirements"] = {"model": {"kind": "model", "capabilities": ["text"]}}
    if opts_in:
        # The built-in Agent's opt-in: only such a generator is ever raised.
        metadata["adopts_harness_grant"] = True
    manifest.content = yaml.safe_dump(metadata)
    schema = next(f for f in source.files if f.name.endswith("settings.set_yaml"))
    schema.content = yaml.safe_dump({"fields": [
        {"key": "surface", "type": "dropdown", "options": ["annotation", "chat"], "default": "annotation"},
        {"key": "backend", "type": "dropdown", "options": ["pydantic-ai", "codex", "qwen-code"], "account_role": "model",
         "account_property": "metadata.harness", "default": "pydantic-ai"},
    ]})
    body = draft()
    body["runner"]["settings"] = {"settings.set_yaml": {"surface": surface, "backend": "pydantic-ai"}}
    body["runner"]["accounts"] = {"model": account["id"]}
    body["runner"]["permissions"] = permissions
    saved = client.post("/api/runners", json=body)
    assert saved.status_code == 200, saved.text


@pytest.mark.parametrize("harness,surface,permissions,adopted,docker,runtime", [
    # A container harness on a runner granting no Docker: the run adopts it.
    ("qwen-code", "annotation", {}, True, "full", "docker"),
    ("codex", "annotation", {"commands": "ask"}, True, "full", "docker"),
    # A chat runner's grant is the user's own; its config check refuses it.
    ("qwen-code", "chat", {}, False, "none", ""),
    # An explicit grant that already places the harness is kept as it is.
    ("qwen-code", "annotation", {"docker": "full", "commands": "allow"}, False, "full", "docker"),
    # pydantic-ai is an adapter on this machine: docker: none serves it, so
    # a runner on docker: limited is never raised; its config check refuses it.
    ("pydantic-ai", "annotation", {}, False, "none", "host"),
    ("pydantic-ai", "annotation", {"docker": "limited", "internet": "full"}, False, "limited", ""),
])
def test_an_annotation_run_adopts_its_harness_minimum_grant_visibly(
        runner_app, registry, monkeypatch, harness, surface, permissions, adopted, docker, runtime):
    import routes.runs.create as create
    import run_registry as run_registry
    from database import get_db_connection
    client, _, _, target = runner_app
    _placement_setup(runner_app, harness, surface, permissions)
    managers, cards = [], []

    class Manager:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            managers.append(self)

        async def start(self):
            pass

    async def reap(*args):
        pass

    real_write = create.write_run_start

    def write_run_start(channel, cell_id, card, placeholders):
        cards.append(card)
        return real_write(channel, cell_id, card, placeholders)

    monkeypatch.setattr(create, "ProcessManager", Manager)
    monkeypatch.setattr(create, "_reap_process", reap)
    monkeypatch.setattr(create, "write_run_start", write_run_start)
    monkeypatch.setattr(create, "active_managers", {})
    monkeypatch.setattr(run_registry, "active_managers", {})
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": "example", "cell_id": str(target.id)})
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    wait_spawned(run_id)
    manager = managers[0]
    # The run runs under the effective grant: the generator's
    # COLLIMATE_PERMISSIONS is the manager's permissions.
    assert manager.permissions.docker == docker
    assert (manager.effective_grant["docker"], manager.adopted) == (docker, adopted)
    conn = get_db_connection(client.channel_id)
    try:
        snapshot = json.loads(conn.execute("SELECT runner_snapshot FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        blob = conn.execute("SELECT blob_hash FROM run_artifacts WHERE run_id=? AND filepath='run.yaml'",
                            (run_id,)).fetchone()[0]
    finally:
        conn.close()
    run_yaml = yaml.safe_load(Path(".collimate/objects", blob[:2], blob[2:]).read_text(encoding="utf-8"))
    for record in (run_yaml, snapshot):
        placement = record["placement"]
        assert record["permissions"]["docker"] == docker == placement["effective_grant"]["docker"]
        assert (placement["harness"], placement["surface"], placement["adopted"]) == (harness, surface, adopted)
        assert placement["runtime"] == runtime and placement["runner_grant"]["docker"] == permissions.get("docker", "none")
        assert placement["raised"] == ([{"axis": "docker", "from": permissions.get("docker", "none"), "to": "full"}]
                                       if adopted else [])
    card = cards[0]["placement"]
    assert card["adopted"] is adopted and card["effective_grant"]["docker"] == docker
    assert ("raised" in card) is adopted


def test_a_generator_that_does_not_opt_in_is_never_raised(runner_app, registry, monkeypatch):
    """A third-party generator binding a field to metadata.harness keeps its
    runner's grant: only a manifest declaring adopts_harness_grant: true (the
    built-in Agent's) is raised, at start or anywhere else."""
    import routes.runs.create as create
    import run_registry as run_registry
    client, _, _, target = runner_app
    _placement_setup(runner_app, "qwen-code", "annotation", {}, opts_in=False)
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
    wait_spawned(response.json()["run_id"])
    manager = managers[0]
    assert manager.permissions.docker == "none" and not manager.adopted


@pytest.mark.parametrize("declared,adopted", [
    (None, False), (False, False), ("true", False), (1, False), (True, True),
])
def test_only_a_literal_true_opt_in_adopts(runner_app, declared, adopted):
    import runners
    from runner_permissions import RunnerPermissions
    manifest = {} if declared is None else {"adopts_harness_grant": declared}
    resolved = runners.ResolvedRunner(
        runner_id="r", runner_name="R", runner_cell_id="", runner_dir="", generator_id="third-party",
        settings_overlay={"settings.set_yaml": {"surface": "annotation", "backend": "qwen-code"}}, folders=[],
        permissions=RunnerPermissions.from_definition({}), packages={}, prompt="", accounts={}, account_requirements={},
        revision="", files=[], generator_revision="", main_config="settings.set_yaml", main_config_content="",
        fields=[{"key": "surface"}, {"key": "backend", "account_role": "model", "account_property": "metadata.harness"}],
        defaults={}, generator_manifest=manifest, definition={})
    placement = runners.adopt_harness_grant(resolved)
    assert (placement is not None and placement["adopted"]) is adopted
    assert resolved.permissions.docker == ("full" if adopted else "none")


def test_a_run_no_harness_binds_records_no_placement(runner_app, monkeypatch):
    import runners
    from runner_permissions import RunnerPermissions
    resolved = runners.ResolvedRunner(
        runner_id="r", runner_name="R", runner_cell_id="", runner_dir="", generator_id="echo",
        settings_overlay={"settings.set_yaml": {"message": "hi"}}, folders=[], permissions=RunnerPermissions.from_definition({}),
        packages={}, prompt="", accounts={}, account_requirements={}, revision="", files=[], generator_revision="",
        main_config="settings.set_yaml", main_config_content="", fields=[{"key": "message"}], defaults={},
        generator_manifest={}, definition={})
    assert runners.adopt_harness_grant(resolved) is None
    assert resolved.permissions.docker == "none"
