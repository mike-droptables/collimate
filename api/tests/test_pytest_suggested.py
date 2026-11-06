"""The suggested catalog, ``GET /api/suggested``: the suggestions of the
content tree installed with the server, a checkout's own ``content/`` when
it runs from one.

Every tree is synthetic (tests/suggested_trees.py) unless a test reads the
checkout's own; ``use_content`` makes a synthetic tree the installed one.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

from suggested_trees import build_tree, create_suggested, provenance, use_content

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def app(api_app):
    from fastapi.testclient import TestClient
    with TestClient(api_app) as client:
        yield client


def _named(root: Path, name: str) -> Path:
    """``root`` with its one suggestion renamed ``name``."""
    path = root / "suggested" / "basic.json"
    suggestion = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**suggestion, "name": name}), encoding="utf-8")
    return root


def _names(catalog: dict) -> list[str]:
    return [s["name"] for s in catalog["suggestions"]]


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

def test_catalog_reads_the_content_tree_with_runner_generators_and_drafts(app, tmp_path, monkeypatch):
    root = build_tree(tmp_path / "sdk")
    catalog = use_content(monkeypatch, app, root)
    assert catalog["error"] is None and set(catalog) == {"suggestions", "error"}
    (entry,) = catalog["suggestions"]
    assert (entry["id"], entry["name"], entry["description"]) == ("basic", "Basic", "Synthetic suggestion")
    assert entry["generators"] == [{"name": "First", "folder": "generators/first"},
                                   {"name": "Second", "folder": "generators/second"}]
    assert entry["renderers"] == [{"name": "Views", "folder": "renderers/views"}]
    assert entry["tutorial"] == [{"name": "Tour", "folder": "suggested/tutorial/tour"}]
    assert [g["id"] for g in entry["runner_generators"]] == ["alpha", "beta"]
    alpha = entry["runner_generators"][0]
    assert alpha["main_config"] == "settings.set_yaml" and alpha["prompt_required"] is True
    assert alpha["default"]["runner"]["settings"] == {"settings.set_yaml": {"message": "hello"}}
    assert sorted(row["draft"]["runner"]["id"] for row in entry["runners"]) == ["needs", "ready"]
    assert all(row["draft"]["files"] == [] and row["quick_fields"] == [] for row in entry["runners"])
    assert app.get("/api/suggested").json() == catalog


def test_quick_fields_wrap_drafts_in_order_without_changing_the_create_body(app, tmp_path, monkeypatch):
    root = build_tree(tmp_path / "sdk")
    manifest = root / "generators" / "first" / "alpha" / "choice.generator"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["account_requirements"] = {"model": {"kind": "model", "required": False}}
    data["prompt_show_if"] = {"enabled": True}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    config = root / "generators" / "first" / "alpha" / "settings.set_yaml"
    config.write_text("fields:\n  - key: enabled\n    type: checkbox\n    default: false\n"
                      "  - key: message\n    type: text\n    default: hello\n", encoding="utf-8")
    ready_path = root / "generators" / "first" / "ready" / "ready.runner"
    ready = yaml.safe_load(ready_path.read_text(encoding="utf-8"))
    ready["settings"]["settings.set_yaml"]["enabled"] = False
    ready_path.write_text(yaml.safe_dump(ready), encoding="utf-8")
    path = root / "suggested" / "basic.json"
    suggestion = json.loads(path.read_text(encoding="utf-8"))
    suggestion["quick_fields"] = {"ready": [
        {"account": "model"}, {"setting": "message"}, {"prompt": True},
    ]}
    path.write_text(json.dumps(suggestion), encoding="utf-8")

    (entry,) = use_content(monkeypatch, app, root)["suggestions"]
    rows = {row["draft"]["runner"]["id"]: row for row in entry["runners"]}
    assert rows["ready"]["quick_fields"] == suggestion["quick_fields"]["ready"]
    assert rows["needs"]["quick_fields"] == []
    assert set(rows["ready"]) == {"draft", "quick_fields"}
    created = create_suggested(app, "With quick fields", runners=[rows["ready"]["draft"]])
    assert created.status_code == 200, created.text
    assert create_suggested(app, "Wrapped draft", runners=[rows["ready"]]).status_code == 400


def test_suggested_create_refuses_an_ineligible_inline_account_id(app, tmp_path, monkeypatch):
    root = build_tree(tmp_path / "sdk")
    manifest = root / "generators" / "first" / "alpha" / "choice.generator"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["account_requirements"] = {"model": {"kind": "model", "required": False}}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    suggestion_path = root / "suggested" / "basic.json"
    suggestion = json.loads(suggestion_path.read_text(encoding="utf-8"))
    suggestion["quick_fields"] = {"ready": [{"account": "model"}]}
    suggestion_path.write_text(json.dumps(suggestion), encoding="utf-8")

    (entry,) = use_content(monkeypatch, app, root)["suggestions"]
    (row,) = [row for row in entry["runners"] if row["draft"]["runner"]["id"] == "ready"]
    row["draft"]["runner"]["accounts"] = {"model": "missing-account"}
    response = create_suggested(app, "Invalid account", runners=[row["draft"]])
    assert response.status_code == 400 and "missing-account" in response.json()["detail"]
    assert app.get("/api/channels").json()["channels"] == []


def test_suggested_create_checks_prompt_gate_after_account_fields_are_derived(app, tmp_path, monkeypatch):
    root = build_tree(tmp_path / "sdk")
    manifest = root / "generators" / "first" / "alpha" / "choice.generator"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["account_requirements"] = {"model": {"kind": "model", "required": True}}
    data["prompt_required"] = False
    data["prompt_show_if"] = {"mode": "on"}
    data["prompt_required_if"] = {"mode": "on"}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    config = root / "generators" / "first" / "alpha" / "settings.set_yaml"
    config.write_text("fields:\n  - key: message\n    type: text\n    default: hello\n"
                      "  - key: mode\n    type: dropdown\n    options: ['off', 'on']\n    default: 'off'\n"
                      "    account_role: model\n    account_property: name\n", encoding="utf-8")
    needs_path = root / "generators" / "first" / "needs" / "needs.runner"
    needs = yaml.safe_load(needs_path.read_text(encoding="utf-8"))
    needs["settings"]["settings.set_yaml"]["mode"] = "off"
    needs["accounts"] = {"model": "chosen-model"}
    needs_path.write_text(yaml.safe_dump(needs), encoding="utf-8")
    suggestion_path = root / "suggested" / "basic.json"
    suggestion = json.loads(suggestion_path.read_text(encoding="utf-8"))
    suggestion["quick_fields"] = {"needs": [{"account": "model"}, {"prompt": True}]}
    suggestion_path.write_text(json.dumps(suggestion), encoding="utf-8")

    (entry,) = use_content(monkeypatch, app, root)["suggestions"]
    (row,) = [row for row in entry["runners"] if row["draft"]["runner"]["id"] == "needs"]
    assert row["draft"]["runner"]["prompt"] == ""
    import collimate_server.accounts as accounts
    monkeypatch.setattr(accounts, "resolve_public_accounts",
                        lambda bindings, requirements: {"model": {"name": "on"}})
    response = create_suggested(app, "Account reveals prompt", runners=[row["draft"]])
    assert response.status_code == 400 and "Prompt" in response.json()["detail"]
    assert app.get("/api/channels").json()["channels"] == []


@pytest.mark.parametrize("quick_fields, error", [
    ({"missing": [{"prompt": True}]}, "unknown packaged runner"),
    ({"ready": [{"account": "missing"}]}, "unknown account role"),
    ({"ready": [{"setting": "missing"}]}, "unknown main-config field"),
    ({"ready": [{"setting": "derived"}]}, "comes from an account"),
    ({"ready": [{"setting": "structured"}]}, "not a scalar main-config control"),
    ({"ready": [{"setting": "outside"}]}, "not a scalar main-config control"),
])
def test_quick_fields_reject_unknown_or_uneditable_schema_targets(app, tmp_path, quick_fields, error, monkeypatch):
    root = build_tree(tmp_path / "sdk")
    manifest = root / "generators" / "first" / "alpha" / "choice.generator"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["account_requirements"] = {"model": {"kind": "model", "required": False}}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    config = root / "generators" / "first" / "alpha" / "settings.set_yaml"
    config.write_text("fields:\n  - key: message\n    type: text\n    default: hello\n"
                      "  - key: derived\n    type: text\n    account_role: model\n    account_property: name\n"
                      "  - key: structured\n    type: list\n"
                      "  - key: outside\n    type: text\n    top_level: true\n", encoding="utf-8")
    path = root / "suggested" / "basic.json"
    suggestion = json.loads(path.read_text(encoding="utf-8"))
    suggestion["quick_fields"] = quick_fields
    path.write_text(json.dumps(suggestion), encoding="utf-8")

    catalog = use_content(monkeypatch, app, root)
    assert catalog["suggestions"] == []
    assert error in catalog["error"] and "quick_fields" in catalog["error"]

def test_an_unreadable_catalog_answers_its_error_and_no_suggestions(app, tmp_path, monkeypatch):
    root = build_tree(tmp_path / "sdk")
    use_content(monkeypatch, app, root)
    shutil.rmtree(root / "generators" / "second")
    catalog = app.get("/api/suggested").json()
    assert catalog["suggestions"] == []
    assert "generators/second" in catalog["error"]


def test_a_malformed_cell_yaml_is_an_error_not_a_failure(app, tmp_path, monkeypatch):
    root = build_tree(tmp_path / "sdk")
    use_content(monkeypatch, app, root)
    (root / "generators" / "second" / "cell.yaml").write_text("name: [", encoding="utf-8")
    response = app.get("/api/suggested")
    assert response.status_code == 200, response.text
    assert response.json()["suggestions"] == [] and response.json()["error"]
    created = create_suggested(app, "Broken")
    assert created.status_code == 400 and "Cannot read the suggestion" in created.json()["detail"]


def test_runner_drafts_come_from_both_asset_trees(app, tmp_path, monkeypatch):
    """A runner package in a renderer-tree cell is offered like one in a
    generator cell, and a kept draft lands in its generator's cell."""
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.state import state
    from suggested_trees import runner, write
    root = build_tree(tmp_path / "sdk")
    write(root / "renderers" / "views" / "beside" / "beside.runner", yaml.safe_dump(runner("beside"), sort_keys=False))
    (entry,) = use_content(monkeypatch, app, root)["suggestions"]
    drafts = {row["draft"]["runner"]["id"]: row["draft"] for row in entry["runners"]}
    assert set(drafts) == {"beside", "needs", "ready"}
    created = create_suggested(app, "Both trees", runners=[drafts["beside"]])
    assert created.status_code == 200, created.text
    channel = next(c for c in state.channels if str(c.id) == created.json()["id"])
    files = {c.name: {f.name for f in c.files} for c in capabilities_stack_cells(channel)}
    assert "runner-beside/beside.runner" in files["First"]
    assert not any(name.endswith(".runner") for name in files["Views"])

# ---------------------------------------------------------------------------
# The bundled tree
# ---------------------------------------------------------------------------

def test_a_channel_starts_from_the_content_tree_and_records_it(app, tmp_path, monkeypatch):
    root = _named(build_tree(tmp_path / "content"), "Installed")
    catalog = use_content(monkeypatch, app, root)
    assert catalog["error"] is None and _names(catalog) == ["Installed"]
    created = create_suggested(app, "From the content tree", tutorial=False)
    assert created.status_code == 200, created.text
    names = {cell["name"] for stack in created.json()["nav_stacks"] + created.json()["main_stacks"]
             for cell in stack["cells"]}
    assert {"First", "Second", "Views"} <= names
    cell = created.json()["nav_stacks"][0]["cells"][-1]
    assert {(r["upstream_kind"], r["upstream_id"]) for r in provenance(created.json()["id"], cell["id"])} == {
        ("app", "")}


def test_a_checkout_bundles_its_own_content_and_builtins(api_app):
    """Without the wheel's package data, the bundled trees are the
    checkout's ``content/`` and ``builtins/``, and the catalog reads the
    first-party suggestions from there."""
    import importlib.util

    import collimate_server.genesis as genesis
    import collimate_server.suggested as suggested
    from fastapi.testclient import TestClient
    assert importlib.util.find_spec("collimate_content") is None
    assert genesis.bundled_dir("content") == str(REPO / "content")
    assert genesis.bundled_dir("builtins") == str(REPO / "builtins")
    assert (genesis.GENERATORS_DIR, genesis.RENDERERS_DIR, genesis.BUILTIN_DIR) == (
        str(REPO / "content" / "generators"), str(REPO / "content" / "renderers"), str(REPO / "builtins"))
    assert suggested.content_root() == REPO / "content"
    with TestClient(api_app) as client:
        catalog = client.get("/api/suggested").json()
    assert catalog["error"] is None and "standard" in [s["id"] for s in catalog["suggestions"]]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.load(response)


def test_a_server_run_from_the_checkout_reads_its_trees_in_place(tmp_path):
    """``python -m collimate_server`` from this checkout's environment, as
    build/dev_server.sh, build/dev_desktop.sh and COLLIMATE_SERVER_SPEC=local:
    run it, serves this checkout's ``content/`` and ``builtins/`` directly:
    /ready reports the bundled trees as the checkout's, and the catalog holds
    the checkout's suggestions. No copy, no override, no fetch."""
    env = {key: os.environ[key] for key in ("PATH", "LANG", "SSL_CERT_FILE") if key in os.environ}
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "TMPDIR"):
        (tmp_path / key).mkdir()
        env[key] = str(tmp_path / key)
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    port = _free_port()
    log = (tmp_path / "server.log").open("wb")
    child = subprocess.Popen([sys.executable, "-I", "-m", "collimate_server", "--port", str(port),
                              "--data-dir", str(tmp_path / "data")],
                             cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        deadline = time.monotonic() + 90
        while True:
            assert child.poll() is None, (tmp_path / "server.log").read_text()
            try:
                ready = _get_json(f"http://127.0.0.1:{port}/ready")
                break
            except OSError:
                assert time.monotonic() < deadline, (tmp_path / "server.log").read_text()
                time.sleep(0.2)
        assert ready["checks"]["bundled"] == "checkout", ready
        catalog = _get_json(f"http://127.0.0.1:{port}/api/suggested")
        shipped = sorted(path.stem for path in (REPO / "content" / "suggested").glob("*.json"))
        assert catalog["error"] is None
        assert sorted(s["id"] for s in catalog["suggestions"]) == shipped
    finally:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        log.close()


def test_an_installed_wheel_bundles_its_package_data(tmp_path, monkeypatch):
    """With the ``collimate`` wheel's ``collimate_content`` and
    ``collimate_builtins`` importable (namespace packages, as the wheel
    installs them), they are the bundled trees, whatever checkout is near."""
    import collimate_server.genesis as genesis
    site = tmp_path / "site-packages"
    for folder in ("collimate_content/generators", "collimate_content/renderers",
                   "collimate_content/suggested", "collimate_builtins/text"):
        (site / folder).mkdir(parents=True)
    monkeypatch.syspath_prepend(str(site))
    assert genesis.bundled_dir("content") == str(site / "collimate_content")
    assert genesis.bundled_dir("builtins") == str(site / "collimate_builtins")
    assert genesis._resolve_assets_dir("renderers") == str(site / "collimate_content" / "renderers")
    assert genesis._resolve_assets_dir("builtin") == str(site / "collimate_builtins")
