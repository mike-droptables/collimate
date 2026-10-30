"""Generator defaults and complete templates exposed to the shared editor."""
from __future__ import annotations

import copy

import pytest
import yaml

from test_pytest_runners import draft, runner_app as runner_app


def test_root_level_config_keeps_supplemental_files(runner_app):
    from models import VirtualFile
    client, _, source, _ = runner_app
    manifest = next(f for f in source.files if f.name == "echo/choice.generator")
    data = yaml.safe_load(manifest.content)
    data["configs"] = [{"id": "root", "name": "Root", "path": "manifest.config_yaml"}]
    manifest.content = yaml.safe_dump(data)
    template = {key: value for key, value in draft()["runner"].items() if key not in ("id", "generator")}
    source.files.extend([
        VirtualFile(name="echo/manifest.config_yaml", content=yaml.safe_dump(template)),
        VirtualFile(name="echo/files/scripts/start.sh", content="echo root", language="text"),
    ])
    response = client.get("/api/runner_generators/echo/configs/root")
    assert response.status_code == 200, response.text
    assert response.json()["files"] == [{"name": "scripts/start.sh", "content": "echo root", "language": "text"}]


@pytest.mark.parametrize("path", ["/api/runner_generators", "/api/runner_generators/echo/configs/default"])
def test_duplicate_generator_keeps_first_editor_template__F0413(runner_app, path):
    client, _, source, _ = runner_app
    duplicate = copy.deepcopy(next(f for f in source.files if f.name == "echo/choice.generator"))
    duplicate.name = "other/choice.generator"
    source.files.extend([duplicate, copy.deepcopy(next(f for f in source.files if f.name == "echo/settings.set_yaml"))])
    source.files[-1].name = "other/settings.set_yaml"
    response = client.get(path)
    assert response.status_code == 200, response.text
    payload = response.json()
    template = payload[0]["default"] if isinstance(payload, list) else payload
    assert template["runner"]["settings"]["settings.set_yaml"]["message"] == "hello"


@pytest.mark.parametrize("field,value", [
    ("configs", ["broken"]), ("configs", [{"id": "missing-path"}]),
    ("configs", None), ("configs", [{"id": [], "name": "Broken", "path": "config"}]),
    ("config_folders", None), ("config_folders", ["scripts"]),
    ("required_files", [None]), ("account_requirements", ["model"]),
    ("account_requirements", {"model": "broken"}),
    ("account_requirements", {"model": {"auth_types": "api_key"}}),
    ("account_requirements", {"model": {"harnesses": [None]}}),
    ("account_requirements", {"model": {"custom_type": []}}),
    ("account_requirements", {"model": {"kind": "modle"}}),
    ("account_requirements", {"model": {"capabilites": ["text"]}}),
    ("prompt_show_if", []), ("prompt_show_if", {}), ("prompt_show_if", None),
    ("prompt_required_if", "session: oneshot"), ("prompt_required_if", {}),
    ("ui_files", ".chat"), ("ui_files", None), ("ui_files", ["chat"]),
    ("ui_files", ["."]), ("ui_files", [3]), ("ui_files", [".a/b"]), ("ui_files", [".run"]),
])
def test_malformed_generator_metadata_is_left_out_and_reported(runner_app, field, value):
    """A malformed generator never takes the other catalogs down: it is left
    out of the editor's generators and reported as a Capabilities problem."""
    import runners
    client, channel, source, _ = runner_app
    manifest = next(f for f in source.files if f.name == "echo/choice.generator")
    data = yaml.safe_load(manifest.content)
    data[field] = value
    manifest.content = yaml.safe_dump(data)
    response = client.get("/api/runner_generators")
    assert response.status_code == 200, response.text
    assert "echo" not in {row["id"] for row in response.json()}
    assert client.get("/api/runners").status_code == 200
    problems = []
    runners.generator_index([source], problems=problems)
    (problem,) = problems
    assert (problem["cell_id"], problem["path"]) == (str(source.id), "echo/choice.generator")
    assert isinstance(problem["error"], str) and problem["error"]


def test_generator_ui_files_are_filename_suffixes(runner_app):
    client, _, source, _ = runner_app
    manifest = next(f for f in source.files if f.name == "echo/choice.generator")
    data = yaml.safe_load(manifest.content)
    data["ui_files"] = [".chat", ".json", ".review_comments"]
    manifest.content = yaml.safe_dump(data)
    response = client.get("/api/runner_generators")
    assert response.status_code == 200, response.text


def test_generator_payload_names_the_axes_the_generator_enforces(runner_app):
    """The runner editor offers a generator-enforced axis (``commands``) only
    for a generator whose manifest declares it (``enforces``); both payloads
    the editor reads carry that declaration, as the SDK reads it
    (``declared_enforces``: generator axes only)."""
    client, _, source, _ = runner_app
    assert client.get("/api/runner_generators").json()[0]["enforces"] == []
    manifest = next(f for f in source.files if f.name == "echo/choice.generator")
    data = yaml.safe_load(manifest.content)
    data["enforces"] = ["commands"]
    manifest.content = yaml.safe_dump(data)
    assert client.get("/api/runner_generators").json()[0]["enforces"] == ["commands"]
    body = draft()
    body["runner"]["permissions"] = {"commands": "ask"}
    editor = client.post("/api/runners", json=body)
    assert editor.status_code == 200, editor.text
    assert editor.json()["generator"]["enforces"] == ["commands"]


def test_generator_exposes_typed_accounts_and_derived_fields(runner_app):
    import runners
    client, _, source, _ = runner_app
    manifest = next(f for f in source.files if f.name == "echo/choice.generator")
    data = yaml.safe_load(manifest.content)
    data["account_requirements"] = {
        "model": {"label": "Agent model", "kind": "model", "protocols": ["openai"], "auth_types": ["api_key"], "harnesses": ["pydantic-ai"]},
        "git": {"kind": "git", "capabilities": ["git"], "required": False},
        "custom": {"kind": "custom", "custom_type": "deployment", "required": False},
    }
    manifest.content = yaml.safe_dump(data)
    config = next(f for f in source.files if f.name == "echo/settings.set_yaml")
    schema = yaml.safe_load(config.content)
    schema["fields"].append({"key": "harness", "type": "text", "account_role": "model", "account_property": "metadata.harness", "default": "pydantic-ai"})
    config.content = yaml.safe_dump(schema)
    response = client.get("/api/runner_generators")
    assert response.status_code == 200, response.text
    entry = response.json()[0]
    assert entry["account_requirements"] == data["account_requirements"]
    assert next(f for f in entry["fields"] if f["key"] == "harness")["account_property"] == "metadata.harness"
    schema["fields"][-1]["account_property"] = "secrets.api_key"
    config.content = yaml.safe_dump(schema)
    response = client.get("/api/runner_generators")
    assert response.status_code == 200 and response.json() == []
    problems = []
    runners.generator_index([source], problems=problems)
    assert "public account information" in problems[0]["error"]


@pytest.mark.parametrize("requirement", [{"kind": "nonexistent"}, {"kinds": ["git", "nonexistent"]}])
def test_generator_with_an_unknown_account_kind_is_unlisted(runner_app, requirement):
    import runners
    client, _, source, _ = runner_app
    manifest = next(f for f in source.files if f.name == "echo/choice.generator")
    data = yaml.safe_load(manifest.content)
    data["account_requirements"] = {"workspace": {**requirement, "required": False}}
    manifest.content = yaml.safe_dump(data)
    response = client.get("/api/runner_generators")
    assert response.status_code == 200 and response.json() == []
    problems = []
    runners.generator_index([source], problems=problems)
    assert "unknown account kind" in problems[0]["error"]


def test_implicit_default_tracks_main_edits_and_has_required_files(runner_app):
    from models import VirtualFile
    client, _, source, _ = runner_app
    manifest = next(f for f in source.files if f.name == "echo/choice.generator")
    data = yaml.safe_load(manifest.content)
    data["required_files"] = ["readme.txt"]
    manifest.content = yaml.safe_dump(data)
    source.files.append(VirtualFile(name="echo/configs/default/files/readme.txt", content="required"))
    path = "/api/runner_generators/echo/configs/default"
    loaded = client.get(path)
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["files"][0]["content"] == "required"
    before = loaded.json()["revision"]
    main = next(f for f in source.files if f.name == "echo/settings.set_yaml")
    main.content += "values:\n  message: changed default\n"
    assert client.get(path).json()["revision"] != before


def test_invalid_runner_metadata_remains_safe_to_list(runner_app):
    client, _, source, _ = runner_app
    assert client.post("/api/runners", json=draft()).status_code == 200
    manifest = next(f for f in source.files if f.name.endswith(".runner"))
    value = yaml.safe_load(manifest.content)
    value.update(name={"broken": True}, tags={"broken": True})
    manifest.content = yaml.safe_dump(value)
    response = client.get("/api/runners")
    assert response.status_code == 200
    row = response.json()[0]
    assert row["name"] == "example" and row["tags"] == [] and row["invalid"]
    assert client.get("/api/runners/example").status_code == 400


@pytest.mark.parametrize("field,value", [("blob_hash", 1), ("blob_hash", "../bad"), ("language", []),
                                         ("name", "C:/outside.txt"), ("name", "D:outside.txt")])
def test_bad_file_metadata_never_mutates_source(runner_app, field, value):
    client, _, source, _ = runner_app
    body = draft()
    body["files"][0][field] = value
    before = source.model_dump()
    response = client.post("/api/runners", json=body)
    assert response.status_code == 400, response.text
    assert source.model_dump() == before
