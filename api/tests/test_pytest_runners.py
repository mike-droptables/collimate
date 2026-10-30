"""Forward runner contracts: complete saves, config copies and fixed execution."""
from __future__ import annotations

from fake_manager import FakeManager
from run_spawn import run_row, wait_spawned
import versions
import copy
import json
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cell_placement import capabilities_stack_cells


@pytest.fixture
def runner_app(fresh_cwd, monkeypatch):
    import state
    from models import Channel, Stack, Cell, VirtualFile
    import routes.runners as routes
    schema = "fields:\n  - key: message\n    type: text\n    default: hello\n  - key: extra\n    type: text\n    advanced: true\n    default: detail\n"
    generator = {"id": "echo", "name": "Echo", "summary": "echo", "entrypoint": "echo.py",
                 "main_config": "settings.set_yaml", "draft_files": ["settings.set_yaml"],
                 "account_requirements": {}, "config_folders": [{"name": "scripts", "description": "Scripts"}]}
    source = Cell(name="Generators", files=[
        VirtualFile(name="echo/choice.generator", content=yaml.safe_dump(generator)),
        VirtualFile(name="echo/settings.set_yaml", content=schema),
        VirtualFile(name="echo/echo.py", content="print('ok')\n"),
    ])
    target = Cell(name="Input", files=[VirtualFile(name="notes.txt", content="cell input")])
    channel = Channel(name="Runners", nav_stacks=[Stack(name="Capabilities", stack_type="capabilities", cells=[source])],
                      main_stacks=[Stack(name="Work", cells=[target])])
    monkeypatch.setattr(state.state, "channels", [channel])
    monkeypatch.setattr(state.state, "active_channel_id", channel.id)
    from database import get_db_connection
    from persistence import initialize_files
    with get_db_connection(str(channel.id)) as conn:
        initialize_files(conn,str(source.id),source.files)
        initialize_files(conn,str(target.id),target.files)
    app = FastAPI()
    app.include_router(routes.router)
    import routes.runs.create as create_routes
    import run_registry as registry
    monkeypatch.setattr(registry, "_shutting_down", False)
    app.include_router(create_routes.router)
    import routes.channels as channel_routes
    monkeypatch.setattr(channel_routes, "state", state.state)
    app.include_router(channel_routes.router)
    # Entered, so the loop a run's background spawn runs on outlives the POST.
    with TestClient(app) as client:
        client.channel_id = str(channel.id)
        yield client, channel, source, target


def draft(identity="example"):
    return {"runner": {"id": identity, "generator": "echo", "name": "Example",
            "description": "Read cell files", "tags": ["writing", "review"],
            "prompt": "Read notes.txt and respond.",
            "settings": {"settings.set_yaml": {"message": "saved", "extra": "detail"}},
            "accounts": {}, "folders": ["scripts"], "permissions": {}, "packages": {}},
            "files": [{"name": "scripts/start.sh", "content": "echo original"}]}


def test_runner_uses_public_manifest_builder__F0205_F1282_F1268_F1311(runner_app, monkeypatch):
    import collimate.lint as lint
    client, _, _, _ = runner_app
    original = lint.GeneratorRef.from_manifest
    observed = []

    def build(cls, manifest, *, read_file=None):
        observed.append((manifest["id"], read_file(manifest["main_config"])))
        return original(manifest, read_file=read_file)

    monkeypatch.setattr(lint.GeneratorRef, "from_manifest", classmethod(build))
    response = client.post("/api/runners", json=draft())
    assert response.status_code == 200, response.text
    assert observed and all(identity == "echo" and "key: message" in source for identity, source in observed)


def test_runner_contract_constants_are_sdk_owned__F1283_F1888():
    import collimate.lint as lint
    import runners
    for name in ("RUNNER_FIELDS", "ACCOUNT_REQUIREMENT_FIELDS", "ACCOUNT_REQUIREMENT_LIST_FIELDS",
                 "ACCOUNT_REQUIREMENT_SCALAR_FIELDS", "ACCOUNT_PROPERTY_ROOTS", "ACCOUNT_PROPERTY_FORBIDDEN"):
        assert getattr(runners, name) is getattr(lint, name)


@pytest.mark.parametrize("manifest,permissions", [
    ({}, {"internet": "none"}),
    ({"requires_docker": True}, {}),
    ({"runs_on": "local-machine"}, {"docker": "limited"}),
])
def test_runner_does_not_recheck_sdk_lint_decisions__F1283(runner_app, monkeypatch, manifest, permissions):
    import collimate.lint as lint
    import runners
    _, _, source, _ = runner_app
    target = runners.generator_index([source])["echo"]
    target["manifest"].update(manifest)
    body = draft()
    body["runner"]["permissions"] = permissions
    monkeypatch.setattr(lint, "lint_runner_manifest_data", lambda *args, **kwargs: [])
    runners.validate_definition(target, body["runner"], body["files"])


def test_a_runner_lives_in_the_cell_it_was_created_from(runner_app):
    """Created in a Capabilities cell (the manage view's 'Keep it in'), a
    runner is saved INTO that cell even when its generator lives elsewhere;
    with no cell it goes into the generator's. From then on every
    edit, copy and delete acts on the cell the runner is in — a cell named on
    an update is checked against it, never followed."""
    import runners
    from models import Cell, VirtualFile
    client, channel, source, target = runner_app
    home = Cell(name="My runners", files=[VirtualFile(name="README.md", content="mine\n")])
    channel.nav_stacks[0].cells.append(home)

    body = {**draft("here"), "cell_id": str(home.id)}
    saved = client.post("/api/runners", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["cell_id"] == str(home.id)
    assert any(f.name == "runner-here/here.runner" for f in home.files)
    assert not any(f.name.startswith("runner-here/") for f in source.files)
    # The catalog places it there too — the manage view filters on cell_id.
    assert {r["id"]: r["cell_id"] for r in client.get("/api/runners").json()} == {"here": str(home.id)}

    # No cell → the generator's cell.
    assert client.post("/api/runners", json=draft("there")).json()["cell_id"] == str(source.id)
    # Only a Capabilities cell can hold a runner.
    refused = client.post("/api/runners", json={**draft("nowhere"), "cell_id": str(target.id)})
    assert refused.status_code == 404 and refused.json()["detail"] == "Runners live in a Capabilities cell"
    assert client.post("/api/runners", json={**draft("nowhere"), "cell_id": "not-a-cell"}).status_code == 404

    # An edit finds the runner where it lives and stays there; naming another
    # cell is refused, naming its own is fine.
    edit = {**body, "revision": saved.json()["revision"]}
    edit["runner"]["description"] = "edited"
    assert client.put("/api/runners/here", json={**edit, "cell_id": str(source.id)}).status_code == 400
    updated = client.put("/api/runners/here", json=edit)
    assert updated.status_code == 200, updated.text
    assert updated.json()["cell_id"] == str(home.id)
    manifest = next(f for f in home.files if f.name == "runner-here/here.runner")
    assert yaml.safe_load(manifest.content)["description"] == "edited"
    assert not any(f.name.startswith("runner-here/") for f in source.files)

    # A copy made the way the UI makes one (the editor payload's cell_id)
    # lands beside its original; a delete removes it from that cell.
    copy = {"runner": {**updated.json()["runner"], "id": "here-copy", "name": "Copy"},
            "files": updated.json()["files"], "cell_id": updated.json()["cell_id"]}
    assert client.post("/api/runners", json=copy).json()["cell_id"] == str(home.id)
    assert client.delete("/api/runners/here-copy").json() == {"ok": True}
    assert not any(f.name.startswith("runner-here-copy/") for f in home.files)
    # The runner's own run resolution names its cell, not the generator's.
    resolved = runners.resolve_runner_run(channel, "here")
    assert (resolved.runner_cell_id, resolved.generator_id) == (str(home.id), "echo")


def test_a_new_runner_can_go_into_a_new_cell_at_the_end_of_capabilities(runner_app):
    """``new_cell`` saves a NEW runner into a fresh standard cell appended to
    Capabilities, named after the runner and numbered past every
    Capabilities cell's name (casefolded); a name only the board uses is the
    runner's to take."""
    from database import get_db_connection
    from models import Cell, Stack
    from persistence import snapshot_files
    client, channel, source, _ = runner_app
    stack = channel.nav_stacks[0]

    saved = client.post("/api/runners", json={**draft("fresh"), "new_cell": True})
    assert saved.status_code == 200, saved.text
    created = stack.cells[-1]
    assert saved.json()["cell_id"] == str(created.id) and created is not source
    assert (created.name, created.builtin) == ("Example", False)
    assert {f.name for f in created.files} == {"runner-fresh/fresh.runner", "runner-fresh/files/scripts/start.sh"}
    with get_db_connection(str(channel.id)) as conn:
        assert {f["path"] for f in snapshot_files(conn, str(created.id))} == {f.name for f in created.files}
    assert {r["id"]: r["cell_id"] for r in client.get("/api/runners").json()}["fresh"] == str(created.id)

    # Named like a Capabilities cell: numbered on.
    again = client.post("/api/runners", json={**draft("again"), "new_cell": True})
    assert stack.cells[-1].name == "Example 2" and again.json()["cell_id"] == str(stack.cells[-1].id)
    body = draft("clash")
    body["runner"]["name"] = "generators"
    client.post("/api/runners", json={**body, "new_cell": True})
    assert stack.cells[-1].name == "generators 2"
    channel.main_stacks.append(Stack(name="Board", cells=[Cell(name="Elsewhere")]))
    body = draft("board")
    body["runner"]["name"] = "ELSEWHERE"
    client.post("/api/runners", json={**body, "new_cell": True})
    assert stack.cells[-1].name == "ELSEWHERE"

    # Not with a named cell, and never on an update.
    count = len(stack.cells)
    both = client.post("/api/runners", json={**draft("both"), "new_cell": True, "cell_id": str(source.id)})
    assert both.status_code == 400 and len(stack.cells) == count
    edit = {**draft("fresh"), "revision": saved.json()["revision"], "new_cell": True}
    assert client.put("/api/runners/fresh", json=edit).status_code == 400
    assert len(stack.cells) == count


def test_a_new_cell_whose_runner_commit_fails_leaves_capabilities_as_it_was(runner_app, monkeypatch):
    import runners
    client, channel, _, _ = runner_app
    before = list(channel.nav_stacks[0].cells)

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(runners, "commit_cell_files", fail)
    with pytest.raises(OSError):
        client.post("/api/runners", json={**draft("fresh"), "new_cell": True})
    assert channel.nav_stacks[0].cells == before


def test_home_cell_refuses_cells_off_capabilities_and_the_built_in_cards(runner_app):
    """A runner's home is a Capabilities cell after the built-in cards: a
    board cell and a card are both refused, and a cell moved onto the stack
    becomes a home."""
    import runners
    from models import Cell
    _, channel, source, target = runner_app
    card = Cell(name="Card", builtin=True)
    channel.nav_stacks[0].cells.insert(0, card)
    assert runners.home_cell(channel, str(source.id)) is source
    for cell in (target, card):
        with pytest.raises(runners.RunnerError) as caught:
            runners.home_cell(channel, str(cell.id))
        assert (caught.value.status, caught.value.reason) == (404, "Runners live in a Capabilities cell")
    channel.main_stacks[0].cells.remove(target)
    channel.nav_stacks[0].cells.append(target)
    assert runners.home_cell(channel, str(target.id)) is target


def test_save_revision_and_binary_files_are_committed(runner_app):
    import runners
    from versions import freeze_cell_versions
    from run_staging import stage_runner_files
    from database import get_db_connection
    client, channel, source, _ = runner_app
    body = draft()
    body["files"].append({"name": "image.bin", "content": "AAEC/w==", "encoding": "base64"})
    response = client.post("/api/runners", json=body)
    assert response.status_code == 200, response.text
    editor = response.json()
    assert editor["runner"]["tags"] == ["writing", "review"]
    # The editor gets every grant ladder, the commands axis included.
    frozen = freeze_cell_versions(client.channel_id, "unused", [str(source.id)])
    workspace = Path("workspace")
    stage_runner_files(client.channel_id, frozen[str(source.id)], "runner-example/", ["scripts"], str(workspace))
    assert (workspace / ".collrun/config/folders/scripts/start.sh").read_text() == "echo original"
    assert (workspace / ".collrun/config/files/image.bin").read_bytes() == bytes([0,1,2,255])
    edit = {"runner": editor["runner"], "files": [{"name": "replacement.txt", "content": "new"}], "revision": editor["revision"]}
    edit["runner"]["prompt"] = "New saved prompt"
    edit["runner"]["folders"] = []
    updated = client.put("/api/runners/example", json=edit)
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] != editor["revision"]
    assert [f["name"] for f in updated.json()["files"]] == ["replacement.txt"]
    assert client.put("/api/runners/example", json=edit).status_code == 409
    assert runners.resolve_runner_run(channel, "example").prompt == "New saved prompt"
    conn = get_db_connection(client.channel_id)
    assert conn.execute("SELECT COUNT(*) FROM cell_edges WHERE parent_key='previous_version'").fetchone()[0] >= 2
    conn.close()


def test_loading_a_named_config_is_an_independent_copy(runner_app):
    from models import VirtualFile
    client, _, source, _ = runner_app
    manifest = next(f for f in source.files if f.name == "echo/choice.generator")
    data = yaml.safe_load(manifest.content)
    data["configs"] = [{"id": "review", "name": "Review", "path": "configs/review/manifest.config_yaml"}]
    manifest.content = yaml.safe_dump(data)
    template = {key: value for key, value in draft()["runner"].items() if key not in ("id", "generator")}
    source.files.extend([
        VirtualFile(name="echo/configs/review/manifest.config_yaml", content=yaml.safe_dump(template)),
        VirtualFile(name="echo/configs/review/files/scripts/start.sh", content="echo review", language="text"),
    ])
    loaded = client.get("/api/runner_generators/echo/configs/review")
    assert loaded.status_code == 200, loaded.text
    config = loaded.json()
    assert config["runner"]["prompt"] == draft()["runner"]["prompt"]
    assert "id" not in config["runner"]
    assert config["files"] == [{"name": "scripts/start.sh", "content": "echo review", "language": "text"}]
    saved = client.post("/api/runners", json={
        "runner": {**config["runner"], "id": "example", "generator": "echo"}, "files": config["files"]})
    assert saved.status_code == 200, saved.text
    editor = saved.json()
    editor["runner"]["prompt"] = "Diverged after loading"
    assert client.put("/api/runners/example", json={"runner": editor["runner"],
        "files": editor["files"], "revision": editor["revision"]}).status_code == 200
    again = client.get("/api/runner_generators/echo/configs/review").json()
    assert again["runner"]["prompt"] == draft()["runner"]["prompt"]
    assert again["revision"] == config["revision"]


def test_partial_settings_and_main_file_writes_rejected_without_mutation(runner_app):
    client, _, source, _ = runner_app
    before = source.model_dump()
    body = draft()
    del body["runner"]["settings"]["settings.set_yaml"]["extra"]
    assert client.post("/api/runners", json=body).status_code == 400
    body = draft()
    body["files"] = [{"name": "settings.set_yaml", "content": "fields: []"}]
    assert client.post("/api/runners", json=body).status_code == 400
    body["files"] = [{"name": "../escape", "content": "no"}]
    assert client.post("/api/runners", json=body).status_code == 400
    assert source.model_dump() == before


def test_invocation_override_contract_is_closed():
    from routes.runs.models import RunRequest
    from pydantic import ValidationError
    for key, value in {"prompt":"x", "config_values":{}, "tag":"default", "relay":"x", "accounts":{}, "inputs":{}, "resume": True, "params": {}}.items():
        with pytest.raises(ValidationError):
            RunRequest(runner="example", **{key:value})


def _on_event_loop() -> bool:
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.parametrize("failure_at", ["retain", "last_gate", "construct", "start"])
def test_startup_failure_releases_account_context_and_workspace__F0397(runner_app, monkeypatch, failure_at):
    """Run start retains the account leases on a worker thread (keyring and
    file locks never block the event loop), before the last gate and before
    the manager exists; every failure from there on releases them. The
    start was already accepted, so each ends the run failed."""
    import accounts
    import routes.runs.create as create
    from database import get_db_connection
    client, _, _, target = runner_app
    assert client.post("/api/runners", json=draft()).status_code == 200
    leased = set()
    events = []
    def retain(context, owner):
        events.append(("retain", _on_event_loop()))
        leased.add(owner)
        if failure_at == "retain":
            raise RuntimeError("synthetic account retention failure")
        return []
    gate_checks = []
    def shutting_down():
        gate_checks.append(1)
        return failure_at == "last_gate" and len(gate_checks) > 1
    class FailingManager(FakeManager):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            events.append(("construct", _on_event_loop()))
            if failure_at == "construct":
                raise RuntimeError("synthetic manager construction failure")
        async def start(self):
            raise RuntimeError("synthetic process startup failure")
    monkeypatch.setattr(create, "ProcessManager", FailingManager)
    monkeypatch.setattr(create, "is_shutting_down", shutting_down)
    monkeypatch.setattr(accounts, "retain_account_context", retain)
    monkeypatch.setattr(accounts, "release_run_account_leases", lambda identity: leased.discard(identity))
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": "example",
        "cell_id": str(target.id)})
    assert response.status_code == 200 and response.json()["status"] == "queued", response.text
    wait_spawned(response.json()["run_id"])
    assert leased == set()
    assert events[0] == ("retain", False)
    if failure_at in ("construct", "start"):
        assert [name for name, _ in events] == ["retain", "construct"]
    else:
        assert [name for name, _ in events] == ["retain"]
    assert not list(Path(".collimate/tmp").glob("app_*"))
    assert not list(Path(".collimate/tmp").glob("ws_*"))
    conn = get_db_connection(client.channel_id)
    try:
        status, reason = conn.execute(
            "SELECT status, failure_reason FROM runs WHERE runner_id='example'").fetchone()
    finally:
        conn.close()
    assert status == "failed"
    assert ("shutting down" in reason) if failure_at == "last_gate" else ("synthetic" in reason)


def test_generator_catalog_is_empty_before_a_channel(runner_app, monkeypatch):
    client, _, _, _ = runner_app
    import state
    monkeypatch.setattr(state.state, "channels", [])
    monkeypatch.setattr(state.state, "active_channel_id", None)
    assert client.get("/api/runners").json() == []
    assert client.get("/api/runner_generators").json() == []
    assert client.get("/api/runner_generators/echo/configs/default").status_code == 404


def test_start_stages_exact_saved_runner_and_records_public_provenance(runner_app, monkeypatch):
    import routes.runs.create as create
    import run_registry as registry
    from database import get_db_connection
    from collimate.config_values import parse_set_yaml, resolve_form_values
    client, channel, source, target = runner_app
    managers = {}
    class Manager(FakeManager):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            managers[self.run_id] = self
        async def start(self): pass
    async def reap(*args): pass
    monkeypatch.setattr(create, "ProcessManager", Manager)
    monkeypatch.setattr(create, "_reap_process", reap)
    monkeypatch.setattr(create, "active_managers", {})
    monkeypatch.setattr(registry, "active_managers", {})
    saved = client.post("/api/runners", json=draft()).json()
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner":"example", "runner_revision":saved["revision"], "cell_id":str(target.id)})
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    wait_spawned(run_id)
    manager = managers[run_id]
    workspace = Path(manager.workspace_dir)
    assert (workspace / "notes.txt").read_text() == "cell input"
    baseline=json.loads((workspace / ".collrun/file_revisions.json").read_text())
    assert baseline["notes.txt"] == 1
    assert (workspace / ".collrun/prompt.md").read_text() == draft()["runner"]["prompt"]
    values = resolve_form_values(parse_set_yaml((workspace / ".collrun/config/files/settings.set_yaml").read_text()))[1]
    assert values == draft()["runner"]["settings"]["settings.set_yaml"]
    assert (workspace / ".collrun/config/folders/scripts/start.sh").read_text() == "echo original"
    conn = get_db_connection(client.channel_id)
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    snapshot = json.loads(row["runner_snapshot"])
    assert snapshot["revision"] == saved["revision"]
    assert row["runner_cell_id"] and row["generator_cell_id"]
    assert (manager.runner_id, manager.runner_name) == ("example", "Example")
    assert manager.cell_id == str(target.id)
    assert manager.account_context == {}
    conn.close()


def test_start_runs_the_generator_cell_the_runner_resolved(runner_app, monkeypatch):
    """A stale manifest with the same generator id in an earlier cell (no
    main_config, so the runner never resolves against it) must not be the
    cell a run freezes or executes."""
    import routes.runs.create as create
    import run_registry as registry
    from models import Cell, VirtualFile
    client, channel, source, target = runner_app
    stale = Cell(name="Stale", files=[
        VirtualFile(name="echo/choice.generator", content=yaml.safe_dump({"id": "echo", "name": "Echo"})),
        VirtualFile(name="echo/echo.py", content="print('stale')\n"),
    ])
    channel.nav_stacks[0].cells.insert(0, stale)
    managers = {}
    class Manager(FakeManager):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            managers[self.run_id] = self
        async def start(self): pass
    async def reap(*args): pass
    monkeypatch.setattr(create, "ProcessManager", Manager)
    monkeypatch.setattr(create, "_reap_process", reap)
    monkeypatch.setattr(create, "active_managers", {})
    monkeypatch.setattr(registry, "active_managers", {})
    assert client.post("/api/runners", json=draft()).status_code == 200
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": "example", "cell_id": str(target.id)})
    assert response.status_code == 200, response.text
    wait_spawned(response.json()["run_id"])
    manager = managers[response.json()["run_id"]]
    assert (Path(manager.app_dir) / "echo.py").read_text() == "print('ok')\n"


def _capture_spawns(monkeypatch):
    """Stop every native spawn at the backend, recording the manager and the
    exact env the generator would have started with."""
    from process_manager.backends import NativeSubprocessBackend

    class Stop(Exception):
        pass

    spawned = []

    async def capture(self, manager, *, env, **_):
        spawned.append((manager, dict(env)))
        if manager._ipc_server is not None:
            manager._ipc_server.close()
        raise Stop("spawn captured")

    monkeypatch.setattr(NativeSubprocessBackend, "spawn", capture)
    return spawned


def _assert_host_run_env(env, manager):
    """What the SDK requires of a run connected to a host (COLLIMATE_IPC_PORT
    set): its run id and channel, and a grant naming exactly the eight axes
    that the run parses to the run's own grant, axis for axis."""
    from collimate.permissions import AXES, Grant
    from conftest import sdk_grant
    assert env["COLLIMATE_IPC_PORT"]
    assert env["COLLIMATE_RUN_ID"] == manager.run_id
    assert env["COLLIMATE_CHANNEL"] == manager.channel
    grant = json.loads(env["COLLIMATE_PERMISSIONS"])
    assert set(grant) == set(AXES)
    assert Grant.from_dict(grant) == sdk_grant(manager.permissions)
    for name in ("COLLIMATE_PACKAGES", "COLLIMATE_RUNNER"):
        if name in env:
            assert isinstance(json.loads(env[name]), dict)
    # Always sent: the manifest's UI file suffixes (``[]`` when none).
    assert json.loads(env["COLLIMATE_UI_FILES"]) == list(manager.ui_files)


def test_a_run_is_sent_the_ui_file_types_its_manifest_declares(runner_app, monkeypatch):
    import routes.runs.create as create
    import run_registry as registry
    spawned = _capture_spawns(monkeypatch)
    monkeypatch.setattr(create, "active_managers", {})
    monkeypatch.setattr(registry, "active_managers", {})
    client, _, source, target = runner_app
    manifest = next(f for f in source.files if f.name == "echo/choice.generator")
    data = yaml.safe_load(manifest.content)
    data["ui_files"] = [".chat", ".json"]
    manifest.content = yaml.safe_dump(data)
    saved = client.post("/api/runners", json=draft()).json()
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={
        "runner": "example", "runner_revision": saved["revision"],
        "cell_id": str(target.id)})
    assert response.status_code == 200, response.text
    wait_spawned(response.json()["run_id"])
    assert "spawn captured" in run_row(client.channel_id, response.json()["run_id"])["failure_reason"]
    [(manager, env)] = spawned
    assert manager.ui_files == (".chat", ".json")
    assert json.loads(env["COLLIMATE_UI_FILES"]) == [".chat", ".json"]


def test_edit_during_account_resolution_rejects_start_before_run_row(runner_app, monkeypatch):
    import accounts
    import runners
    from database import get_db_connection
    client, channel, _, target = runner_app
    saved = client.post("/api/runners", json=draft()).json()
    def concurrent_edit(*args):
        definition = copy.deepcopy(saved["runner"])
        definition["prompt"] = "Changed while startup was resolving accounts"
        runners.save_runner(channel, definition, saved["files"], expected_revision=saved["revision"])
        return {}
    monkeypatch.setattr(accounts, "resolve_accounts", concurrent_edit)
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner":"example", "runner_revision":saved["revision"], "cell_id":str(target.id)})
    assert response.status_code == 409, response.text
    conn = get_db_connection(client.channel_id)
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE kind='generator'").fetchone()[0] == 0
    conn.close()
    assert not any(f.name.startswith("_runs/") for f in target.files)


def test_config_types_and_old_secret_permissions_are_rejected(runner_app):
    client, _, _, _ = runner_app
    body = draft()
    body["runner"]["settings"]["settings.set_yaml"]["message"] = ["wrong type"]
    assert client.post("/api/runners", json=body).status_code == 400
    body = draft()
    body["runner"]["permissions"] = {"api_keys": ["anthropic"]}
    assert client.post("/api/runners", json=body).status_code == 400


def test_building_a_process_manager_takes_no_account_lease__F0397(tmp_path, monkeypatch):
    """The manager is built on the event loop right after the last gate, so
    its constructor must not touch the keyring or the lease files."""
    import accounts
    from process_manager.manager import ProcessManager
    from runner_permissions import UNRESTRICTED

    def retain(*_a, **_k):
        raise AssertionError("ProcessManager() retained account leases on the event loop")
    monkeypatch.setattr(accounts, "retain_account_context", retain)
    oauth = {"id": "acct", "service": "openai", "kind": "oauth",
             "secrets": {"refresh_token": "r"}, "metadata": {}}
    manager = ProcessManager(
        channel="00000000-0000-4000-8000-000000000001", run_id="run-f0397", app_dir=str(tmp_path),
        workspace_dir=str(tmp_path), entrypoint="gen.py",
        permissions=UNRESTRICTED, account_context={"model": oauth},
    )
    assert manager.account_context == {"model": oauth}


async def test_a_start_cancelled_mid_retain_waits_for_the_thread_before_unwinding__F0397(monkeypatch):
    """The retain's worker thread cannot be cancelled. A start cancelled while
    it runs waits for it before the cancellation reaches the unwind, so the
    unwind's release sees every lease the thread took."""
    import asyncio
    import threading
    import accounts
    from process_manager.manager import retain_account_leases

    entered, finish = threading.Event(), threading.Event()
    leased = set()

    def slow_retain(context, owner):
        entered.set()
        finish.wait(5)
        leased.add(owner)

    monkeypatch.setattr(accounts, "retain_account_context", slow_retain)
    task = asyncio.create_task(retain_account_leases({}, "run-cancelled"))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.1)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert leased == {"run-cancelled"}


def test_copied_channel_preserves_complete_runner_files_TR05(runner_app):
    import runners
    from models import VirtualFile
    client, channel, source, _ = runner_app
    manifest = next(f for f in source.files if f.name == 'echo/choice.generator')
    data = yaml.safe_load(manifest.content)
    data['configs'] = [{'id': 'custom', 'name': 'Custom', 'path': 'configs/custom/manifest.config_yaml'}]
    manifest.content = yaml.safe_dump(data)
    body = draft()
    body['files'].append({'name':'asset.bin','content':'AAEC/w==','encoding':'base64'})
    template = {key: value for key, value in body['runner'].items() if key not in ('id', 'generator')}
    source.files.extend([
        VirtualFile(name='echo/configs/custom/manifest.config_yaml', content=yaml.safe_dump(template)),
        VirtualFile(name='echo/configs/custom/files/scripts/start.sh', content='echo original', language='text'),
    ])
    saved = client.post('/api/runners',json=body).json()
    created = client.post('/api/channels', params={'name': 'Imported'},
                          json={'config': {'packages': {'channel_id': client.channel_id}}})
    assert created.status_code == 200, created.text
    from state import state
    copied = next(c for c in state.channels if str(c.id) == created.json()['id'])
    assert state.active_channel_id == copied.id
    editor = runners.build_editor(copied,'example')
    assert editor['runner'] == saved['runner']
    assert {f['name'] for f in editor['files']} == {'scripts/start.sh','asset.bin'}
    gi = runners.generator_index(capabilities_stack_cells(copied))['echo']
    template = runners.config_template(gi,'custom')
    assert template['runner']['prompt'] == body['runner']['prompt']
    blob = next(f['blob_hash'] for f in editor['files'] if f['name']=='asset.bin')
    assert Path('.collimate/objects',blob[:2],blob[2:]).read_bytes() == bytes([0,1,2,255])


def _fake_manager(monkeypatch):
    import events
    import routes.runs.create as create
    import run_registry as registry
    managers, broadcasts = {}, []
    class Manager(FakeManager):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            managers[self.run_id] = self
        async def start(self): pass
    async def reap(*args): pass
    monkeypatch.setattr(create, "ProcessManager", Manager)
    monkeypatch.setattr(create, "_reap_process", reap)
    monkeypatch.setattr(create, "active_managers", {})
    monkeypatch.setattr(registry, "active_managers", {})
    monkeypatch.setattr(events, "broadcast", lambda ch, ev: broadcasts.append((ch, ev)))
    return managers, broadcasts


def test_a_start_records_its_recipe_and_is_announced(runner_app, monkeypatch):
    """A start records the runner recipe it resolved in runner_snapshot and
    in the run.yaml config artifact, and is announced as run_started."""
    from database import get_db_connection
    client, _, _, target = runner_app
    managers, broadcasts = _fake_manager(monkeypatch)
    assert client.post("/api/runners", json=draft()).status_code == 200
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={
        "runner": "example", "cell_id": str(target.id)})
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    wait_spawned(run_id)
    assert run_id in managers
    conn = get_db_connection(client.channel_id)
    try:
        snapshot = json.loads(conn.execute(
            "SELECT runner_snapshot FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        blob = conn.execute(
            "SELECT blob_hash FROM run_artifacts WHERE run_id=? AND filepath='run.yaml'",
            (run_id,)).fetchone()[0]
    finally:
        conn.close()
    assert set(snapshot) == {"runner", "name", "generator", "permissions", "settings",
                             "folder_hashes", "accounts", "revision"}
    run_yaml = yaml.safe_load(
        Path(".collimate/objects", blob[:2], blob[2:]).read_text(encoding="utf-8"))
    assert set(run_yaml) == {"runner", "generator", "runner_source_version_id",
                             "permissions", "packages", "settings_overlay",
                             "input_version_ids"}
    label = f"example-{run_id[:8]}"
    started = {
        "run_id": run_id, "cell_id": str(target.id), "runner_id": "example",
        "runner_name": "Example", "label": label, "folder": f"_runs/{label}",
        "generator_name": "echo", "opens": [],
    }
    assert (client.channel_id, {"type": "run_started", "stack_id": None, **started}) in broadcasts
    assert response.json() == {"status": "queued", **started}


def test_a_start_carrying_params_is_a_422_before_any_mutation(runner_app, monkeypatch):
    """A run takes no per-run input: a request with ``params`` is refused at
    the edge, and nothing is recorded or started."""
    from database import get_db_connection
    client, _, _, target = runner_app
    managers, _ = _fake_manager(monkeypatch)
    assert client.post("/api/runners", json=draft()).status_code == 200
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={
        "runner": "example", "cell_id": str(target.id), "params": {"surface": "annotation"}})
    assert response.status_code == 422, response.text
    assert managers == {}
    conn = get_db_connection(client.channel_id)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE runner_id='example'").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_second_run_on_a_live_cell_is_a_normal_start(runner_app, monkeypatch):
    """Several runs may be live on one cell: a start on a cell with a live
    run — or held by another start — goes through, each run with its own
    label and run card."""
    import run_registry as registry
    client, _, _, target = runner_app
    managers, _ = _fake_manager(monkeypatch)
    monkeypatch.setattr(registry, "_holds", {})
    assert client.post("/api/runners", json=draft()).status_code == 200
    registry.reserve("run-starting", str(target.id))
    first = client.post(f"/api/channels/{client.channel_id}/runs", json={
        "runner": "example", "cell_id": str(target.id)})
    second = client.post(f"/api/channels/{client.channel_id}/runs", json={
        "runner": "example", "cell_id": str(target.id)})
    assert first.status_code == 200 and second.status_code == 200, second.text
    wait_spawned(first.json()["run_id"])
    wait_spawned(second.json()["run_id"])
    labels = {first.json()["label"], second.json()["label"]}
    assert len(labels) == 2 and set(managers) == {first.json()["run_id"], second.json()["run_id"]}
    names = {f.name for f in target.files}
    assert {f"_runs/{label}/{label}.run" for label in labels} <= names


@pytest.mark.parametrize("late", ["hold_cancelled", "shutting_down"])
def test_a_start_whose_gate_closed_while_it_prepared_unwinds_before_it_registers(
        runner_app, monkeypatch, late):
    """The spawn awaits between the accept and the registration. Stop all
    (cancelling the start's own hold) or a server shutdown landing in that
    window is caught by the last gate, just before the run goes live:
    nothing registers or spawns, the run row ends stopped (a cancelled
    start) or failed (shutdown) and run_status says so, the run card the
    accept committed is swept again, and the start's hold is released."""
    import asyncio
    import routes.runs.create as create
    import run_registry as registry
    from database import get_db_connection
    from routes.runs.models import RunRequest
    client, channel, _, target = runner_app
    managers, broadcasts = _fake_manager(monkeypatch)
    monkeypatch.setattr(registry, "_holds", {})
    assert client.post("/api/runners", json=draft()).status_code == 200
    real_resolve = create.resolve_optional_requirements

    def resolve_then_close_the_gate(*args):
        if late == "hold_cancelled":
            assert registry.cancel_holds() == 1
        else:
            registry.mark_shutting_down(True)
        return real_resolve(*args)
    monkeypatch.setattr(create, "resolve_optional_requirements", resolve_then_close_the_gate)
    async def start():
        answer = await create.start_run(client.channel_id, RunRequest(
            runner="example", cell_id=str(target.id)))
        await create.spawn_settled(answer["run_id"])
        return answer
    answer = asyncio.run(start())
    assert answer["status"] == "queued"
    assert managers == {}
    assert registry._holds == {}
    status = "stopped" if late == "hold_cancelled" else "failed"
    assert any(ev["type"] == "run_status" and ev["run_id"] == answer["run_id"]
               and ev["status"] == status for _, ev in broadcasts)
    conn = get_db_connection(client.channel_id)
    try:
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM runs WHERE runner_id='example'").fetchall()]
    finally:
        conn.close()
    assert statuses == [status]
    assert not any(f.name.startswith("_runs/") for f in target.files)


def test_a_file_named_like_a_runner_manifest_is_not_a_second_runner(runner_app):
    """A visible package definition is a runner: a runner's own
    attached file of that name, one level deeper, is just a file."""
    import runners
    client, channel, _, _ = runner_app
    body = draft("a")
    body["files"].append({"name": "examples/preset.runner",
                          "content": yaml.safe_dump(body["runner"])})
    assert client.post("/api/runners", json=body).status_code == 200
    catalog = runners.build_catalog(channel)
    assert [r["id"] for r in catalog] == ["a"]
    assert "invalid" not in catalog[0]
    assert "available_ui" not in catalog[0] and "callable_by_generators" not in catalog[0]


def test_a_binary_runner_file_is_committed_by_hash_not_rehashed(runner_app, monkeypatch):
    import hashlib
    from database import get_db_connection
    client, _, _, _ = runner_app
    rehashed = []
    real = versions.store_bytes
    monkeypatch.setattr(versions, "store_bytes", lambda raw: rehashed.append(raw) or real(raw))
    body = draft()
    body["files"].append({"name": "image.bin", "content": "AAEC/w==", "encoding": "base64"})
    saved = client.post("/api/runners", json=body)
    assert saved.status_code == 200, saved.text
    assert bytes([0, 1, 2, 255]) not in rehashed
    conn = get_db_connection(client.channel_id)
    try:
        hashes = {r[0] for r in conn.execute(
            "SELECT blob_hash FROM cell_files WHERE filepath='runner-example/files/image.bin'").fetchall()}
    finally:
        conn.close()
    assert hashes == {hashlib.sha256(bytes([0, 1, 2, 255])).hexdigest()}
    # The editor hands the file back by hash; saving that payload again works.
    edit = {"runner": saved.json()["runner"], "files": saved.json()["files"], "revision": saved.json()["revision"]}
    edit["runner"]["prompt"] = "Again"
    assert client.put("/api/runners/example", json=edit).status_code == 200


def test_another_runner_saved_beside_a_starting_one_is_not_a_generator_change(runner_app):
    import runners
    from models import VirtualFile
    client, channel, source, _ = runner_app
    assert client.post("/api/runners", json=draft("example")).status_code == 200
    assert client.post("/api/runners", json=draft("other")).status_code == 200
    resolved = runners.resolve_runner_run(channel, "example")
    assert client.post("/api/runners", json=draft("third")).status_code == 200
    assert client.delete("/api/runners/other").status_code == 200
    runners.verify_runner_start(channel, resolved)
    source.files = [VirtualFile(name=f.name, content="print('changed')\n") if f.name == "echo/echo.py" else f
                    for f in source.files]
    with pytest.raises(runners.RunnerError) as exc:
        runners.verify_runner_start(channel, resolved)
    assert exc.value.status == 409


def test_duplicate_generator_only_invalidates_dependent_runners__F0413(runner_app, caplog):
    from models import Cell
    import runners

    client, channel, source, target = runner_app
    assert client.post('/api/runners', json=draft('dependent')).status_code == 200
    other = Cell(name='Other generator', files=copy.deepcopy(runners.without_runners(source.files)))
    manifest = next(file for file in other.files if file.name.endswith('choice.generator'))
    definition = yaml.safe_load(manifest.content)
    definition['id'] = 'other'
    manifest.content = yaml.safe_dump(definition)
    channel.nav_stacks[0].cells.append(other)
    other_draft = draft('independent')
    other_draft['runner']['generator'] = 'other'
    assert client.post('/api/runners', json=other_draft).status_code == 200
    duplicate = Cell(name='Duplicate', files=copy.deepcopy(runners.without_runners(source.files)))
    channel.nav_stacks[0].cells.append(duplicate)

    response = client.get('/api/runners')
    assert response.status_code == 200, response.text
    entries = {entry['id']: entry for entry in response.json()}
    assert 'Duplicate generator id' in entries['dependent']['invalid']['reason']
    assert 'invalid' not in entries['independent']
    assert runners.generator_index(capabilities_stack_cells(channel))['echo']['cell'] is source
    assert 'Duplicate generator id' in caplog.text
    assert client.get('/api/runner_generators').status_code == 200
    response = client.post(f'/api/channels/{client.channel_id}/runs', json={'runner': 'dependent', 'cell_id': str(target.id)})
    assert response.status_code == 409 and 'Duplicate generator id' in response.text


def test_resolved_runner_carries_fields_without_reparsing__F0412_F0466(runner_app, monkeypatch):
    import runners

    client, channel, _, _ = runner_app
    assert client.post('/api/runners', json=draft()).status_code == 200
    resolved = runners.resolve_runner_run(channel, 'example')
    assert [field['key'] for field in resolved.fields] == ['message', 'extra']
    assert resolved.defaults == {'message': 'hello', 'extra': 'detail'}
    monkeypatch.setattr('collimate.config_values.parse_set_yaml', lambda *_: pytest.fail('resolved config must not be parsed again'))
    runners.apply_account_settings(resolved, {})


def test_permission_vocabulary_for_new_forms__F0948(runner_app, monkeypatch):
    from runner_permissions import CAPABILITY_ENUMS, RunnerPermissions
    client, _, _, _ = runner_app
    response = client.get('/api/runner_vocabulary')
    assert response.status_code == 200
    vocabulary = response.json()
    assert vocabulary['enums'] == {axis: list(levels) for axis, levels in CAPABILITY_ENUMS.items()}
    assert vocabulary['defaults'] == RunnerPermissions.from_definition({}).to_dict()
    assert set(vocabulary['labels']) == set(vocabulary['defaults'])
    assert list(vocabulary['descriptions']['docker']) == vocabulary['enums']['docker']
    assert all(vocabulary['descriptions']['docker'].values())


@pytest.mark.parametrize("config_id", ["default", "custom"])
def test_public_config_template_preserves_files_revision_and_route__F0450(runner_app, config_id):
    import runners
    from models import VirtualFile

    client, channel, source, _ = runner_app
    manifest = next(f for f in source.files if f.name == "echo/choice.generator")
    data = yaml.safe_load(manifest.content)
    data["configs"] = [{"id": "custom", "name": "Custom", "path": "configs/custom/manifest.config_yaml"}]
    manifest.content = yaml.safe_dump(data)
    custom = {key: value for key, value in draft()["runner"].items() if key not in ("id", "generator")}
    source.files.extend([
        VirtualFile(name="echo/configs/custom/manifest.config_yaml", content=yaml.safe_dump(custom)),
        VirtualFile(name="echo/configs/custom/files/scripts/start.sh", content="echo custom", language="text"),
        VirtualFile(name="echo/configs/default/files/scripts/start.sh", content="echo default", language="text"),
    ])
    gi = runners.generator_index(capabilities_stack_cells(channel))["echo"]
    template = runners.config_template(gi, config_id)
    assert template["files"] == [{"name": "scripts/start.sh", "content": f"echo {config_id}", "language": "text"}]
    assert template["runner"]["settings"] == {
        "settings.set_yaml": {"message": "saved" if config_id == "custom" else "hello", "extra": "detail"},
    }
    assert template["revision"] == runners.config_template(gi, config_id)["revision"]
    response = client.get(f"/api/runner_generators/echo/configs/{config_id}")
    assert response.status_code == 200, response.text
    assert response.json() == template
    source_file = next(f for f in source.files if f.name == f"echo/configs/{config_id}/files/scripts/start.sh")
    source_file.content = "echo changed"
    changed = runners.config_template(runners.generator_index(capabilities_stack_cells(channel))["echo"], config_id)
    assert changed["revision"] != template["revision"]


def test_arbitrary_runner_edit_stage_delete_and_source_revision__D04(runner_app, tmp_path):
    import runners
    from versions import freeze_cell_versions
    from run_staging import stage_runner_files
    from models import VirtualFile
    client, channel, source, _ = runner_app
    body = draft("placed")
    definition_path = "deep/custom/unrelated.runner"
    source.files.extend(runners.runner_vfiles(body["runner"], body["files"],
        occupied_files=source.files, definition_path=definition_path))
    editor = client.get("/api/runners/placed").json()
    body["runner"]["description"] = "edited at the original path"
    response = client.put("/api/runners/placed", json={**body, "revision": editor["revision"]})
    assert response.status_code == 200, response.text
    assert any(f.name == definition_path for f in source.files)
    assert not any(f.name.startswith("runner-placed/") for f in source.files)
    resolved = runners.resolve_runner_run(channel, "placed")
    assert resolved.runner_dir == "deep/custom/"
    frozen = freeze_cell_versions(client.channel_id, "unused", [str(source.id)])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stage_runner_files(client.channel_id, frozen[str(source.id)], resolved.runner_dir, ["scripts"], str(workspace))
    assert (workspace / ".collrun/config/folders/scripts/start.sh").read_text() == "echo original"
    source.files.extend([VirtualFile(name="unrelated/view.renderer", content="id: different\n"),
                         VirtualFile(name="unrelated/main.tsx", content="other")])
    runners.verify_runner_start(channel, resolved)
    source.files.append(VirtualFile(name="_shared/helper.py", content="changed shared"))
    with pytest.raises(runners.RunnerError, match="Generator changed"):
        runners.verify_runner_start(channel, resolved)
    assert client.delete("/api/runners/placed").status_code == 200
    assert not any(f.name.startswith("deep/custom/") for f in source.files)
    assert any(f.name == "unrelated/main.tsx" for f in source.files)


@pytest.mark.parametrize('required_path',['notes.txt','runner-example/files/scripts/start.sh'])
@pytest.mark.parametrize('damage',['missing','corrupt'])
def test_run_refuses_damaged_required_workspace_or_runner_body(runner_app,monkeypatch,required_path,damage):
    import run_staging
    import run_registry
    from cas import blob_path
    client,channel,_,target=runner_app
    managers,_=_fake_manager(monkeypatch)
    assert client.post('/api/runners',json=draft()).status_code==200
    copy=run_staging._copy_required_blob
    damaged=[]
    def corrupt_frozen_input(digest,dest,name):
        if name==required_path:
            path=Path(blob_path(digest))
            path.unlink() if damage=='missing' else path.write_bytes(b'damaged after capture')
            damaged.append(name)
        return copy(digest,dest,name)
    monkeypatch.setattr(run_staging,'_copy_required_blob',corrupt_frozen_input)
    response=client.post(f'/api/channels/{channel.id}/runs',json={'runner':'example','cell_id':str(target.id)})
    assert response.status_code==200,response.text
    wait_spawned(response.json()['run_id'])
    row=run_row(str(channel.id),response.json()['run_id'])
    assert row['status']=='failed' and required_path in row['failure_reason']
    assert damaged==[required_path]
    assert not managers
    assert str(target.id) not in run_registry.running_cell_ids()
    assert not list(Path('.collimate/tmp').glob('ws_*'))


@pytest.mark.parametrize("extra,message", [
    ({"takes_selected_file": "yes"}, "takes_selected_file must be a boolean"),
    ({"takes_selected_file_if": {}}, "takes_selected_file_if must be a nonempty condition mapping"),
    ({"requires_docker_if": ["surface"]}, "requires_docker_if must be a nonempty condition mapping"),
    ({"opens": {"path": "a.website"}}, "opens must be a list"),
    ({"opens": [{"path": "a.website", "when": {"x": 1}}]}, "needs a path and an optional if"),
    ({"opens": [{"path": "a.website", "if": {}}]}, "if must be a nonempty condition mapping"),
    ({"opens": [{"path": "notes/{run}/a.chat"}]}, "{run} only as its first segment"),
    ({"opens": [{"path": "{run}/chat/main.txt"}]}, "does not end in a declared ui_files suffix"),
    ({"opens": [{"path": "../a.website"}]}, "relative cell file path"),
])
def test_generator_metadata_validates_the_selected_file_docker_and_opens_keys_GN02(extra, message):
    import runners
    manifest = {"id": "g", "main_config": "settings.set_yaml", "ui_files": [".chat"], **extra}
    with pytest.raises(runners.RunnerError) as caught:
        runners._validate_generator_metadata(manifest, "g.generator")
    assert message in caught.value.reason


def test_generator_metadata_accepts_the_agent_shape_GN02():
    import runners
    runners._validate_generator_metadata({
        "id": "agent", "ui_files": [".chat", ".json", ".terminal"],
        "requires_docker_if": {"surface": "terminal"},
        "takes_selected_file_if": {"selected_file_first": True},
        "opens": [
            {"path": "{run}/chat/conversation.chat", "if": {"surface": "chat"}},
            {"path": "{run}/terminal/{backend}.terminal", "if": {"surface": "terminal"}},
        ],
    }, "agent.generator")


def test_saved_runner_validation_honours_requires_docker_if_GN02(runner_app):
    """A runner whose saved settings meet the generator's requires_docker_if
    needs docker: full; with other settings it does not."""
    import runners
    _, _, source, _ = runner_app
    target = runners.generator_index([source])["echo"]
    target["manifest"]["requires_docker_if"] = {"message": "boxed"}
    target["fields"][0].update(type="dropdown", options=["saved", "boxed"])
    schema = ("fields:\n  - key: message\n    type: dropdown\n    options: [saved, boxed]\n"
              "    default: saved\n  - key: extra\n    type: text\n    advanced: true\n    default: detail\n")
    target["files_by_name"]["settings.set_yaml"].content = schema
    body = draft()
    runners.validate_definition(target, body["runner"], body["files"])
    body["runner"]["settings"]["settings.set_yaml"]["message"] = "boxed"
    with pytest.raises(runners.RunnerError) as caught:
        runners.validate_definition(target, body["runner"], body["files"])
    assert "requires docker: full with these settings" in caught.value.reason
    body["runner"]["permissions"] = {"docker": "full"}
    runners.validate_definition(target, body["runner"], body["files"])
