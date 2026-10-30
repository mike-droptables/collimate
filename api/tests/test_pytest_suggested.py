"""The suggested catalog: its source (a folder override, the fetched latest
release content, or the bundled tree), ``GET /api/suggested`` and
``PUT /api/suggested/source``.

Every tree is synthetic (tests/suggested_trees.py) unless a test reads the
checkout's own; the bundled tree is a synthetic one (``bundled``), and the
GitHub fetch is an ``httpx.MockTransport`` serving a ``content.zip`` built
from one: the suite never reaches the network.
"""
from __future__ import annotations

import io
import json
import shutil
import stat
import zipfile
from pathlib import Path

import httpx
import pytest
import yaml

from suggested_trees import build_tree, create_suggested, use_source

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def github(monkeypatch):
    """The release asset the app fetches: set ``github.body`` (bytes) or
    ``github.status``; ``github.calls`` counts the requests."""
    import suggested

    class Remote:
        body: bytes = b""
        status = 200
        calls = 0

    remote = Remote()

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == suggested.GITHUB_CONTENT_ZIP
        remote.calls += 1
        return httpx.Response(remote.status, content=remote.body)

    monkeypatch.setattr(suggested, "_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True))
    return remote


@pytest.fixture
def bundled(api_app, tmp_path, monkeypatch):
    """A synthetic tree standing in for the content installed with the app
    (its suggestion is named ``Bundled``); answers its root."""
    import genesis
    root = _named(build_tree(tmp_path / "bundled"), "Bundled")
    real = genesis.bundled_dir
    monkeypatch.setattr(genesis, "bundled_dir", lambda tree: str(root) if tree == "content" else real(tree))
    return root


@pytest.fixture
def app(api_app, github, bundled):
    from fastapi.testclient import TestClient
    with TestClient(api_app) as client:
        yield client


def _named(root: Path, name: str) -> Path:
    """``root`` with its one suggestion renamed ``name``."""
    path = root / "suggested" / "basic.json"
    suggestion = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**suggestion, "name": name}), encoding="utf-8")
    return root


def _content_zip(tree: Path, *, prefix: str = "", extra: dict[str, bytes] | None = None) -> bytes:
    """``content.zip`` of the content ``tree``: its folders at the zip root
    (under ``prefix`` when one is given), and the ``extra`` members."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                bundle.write(path, prefix + path.relative_to(tree).as_posix())
        for name, body in (extra or {}).items():
            bundle.writestr(name, body)
    return buffer.getvalue()


def _trees() -> list[str]:
    import suggested
    return sorted(p.name for p in Path(suggested.CACHE_DIR).iterdir() if p.name.startswith("github-"))


def _names(catalog: dict) -> list[str]:
    return [s["name"] for s in catalog["suggestions"]]


# ---------------------------------------------------------------------------
# A folder override
# ---------------------------------------------------------------------------

def test_catalog_reads_a_local_folder_with_runner_generators_and_drafts(app, tmp_path):
    root = build_tree(tmp_path / "sdk")
    catalog = use_source(app, root)
    assert catalog["source"] == {"kind": "folder", "path": str(root), "fetched_at": None}
    assert catalog["error"] is None and set(catalog) == {"source", "suggestions", "error"}
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


def test_quick_fields_wrap_drafts_in_order_without_changing_the_create_body(app, tmp_path):
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

    (entry,) = use_source(app, root)["suggestions"]
    rows = {row["draft"]["runner"]["id"]: row for row in entry["runners"]}
    assert rows["ready"]["quick_fields"] == suggestion["quick_fields"]["ready"]
    assert rows["needs"]["quick_fields"] == []
    assert set(rows["ready"]) == {"draft", "quick_fields"}
    created = create_suggested(app, "With quick fields", runners=[rows["ready"]["draft"]])
    assert created.status_code == 200, created.text
    assert create_suggested(app, "Wrapped draft", runners=[rows["ready"]]).status_code == 400


def test_suggested_create_refuses_an_ineligible_inline_account_id(app, tmp_path):
    root = build_tree(tmp_path / "sdk")
    manifest = root / "generators" / "first" / "alpha" / "choice.generator"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["account_requirements"] = {"model": {"kind": "model", "required": False}}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    suggestion_path = root / "suggested" / "basic.json"
    suggestion = json.loads(suggestion_path.read_text(encoding="utf-8"))
    suggestion["quick_fields"] = {"ready": [{"account": "model"}]}
    suggestion_path.write_text(json.dumps(suggestion), encoding="utf-8")

    (entry,) = use_source(app, root)["suggestions"]
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

    (entry,) = use_source(app, root)["suggestions"]
    (row,) = [row for row in entry["runners"] if row["draft"]["runner"]["id"] == "needs"]
    assert row["draft"]["runner"]["prompt"] == ""
    import accounts
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
def test_quick_fields_reject_unknown_or_uneditable_schema_targets(app, tmp_path, quick_fields, error):
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

    response = app.put("/api/suggested/source", json={"path": str(root)})
    assert response.status_code == 400
    assert error in response.json()["detail"] and "quick_fields" in response.json()["detail"]


def test_source_refusals_leave_the_override_unsaved(app, tmp_path, github, bundled):
    import suggested
    root = build_tree(tmp_path / "sdk")
    use_source(app, root)
    (tmp_path / "plain").mkdir()
    for path in ("relative/sdk", str(tmp_path / "missing"), str(tmp_path / "plain"), str(root / "suggested" / "basic.json")):
        response = app.put("/api/suggested/source", json={"path": path})
        assert response.status_code == 400, path
        assert suggested.override() == str(root)
    assert app.put("/api/suggested/source", json={"folder": str(root)}).status_code == 422
    cleared = app.put("/api/suggested/source", json={"path": None})
    assert cleared.status_code == 200
    assert suggested.override() is None
    assert cleared.json()["source"] == {"kind": "bundled", "path": str(bundled), "fetched_at": None}
    assert github.calls == 0


def test_a_folder_ignores_refresh(app, tmp_path, github):
    root = _named(build_tree(tmp_path / "sdk"), "Folder")
    use_source(app, root)
    catalog = app.get("/api/suggested", params={"refresh": 1}).json()
    assert catalog["source"]["kind"] == "folder" and _names(catalog) == ["Folder"]
    assert github.calls == 0


def test_an_unreadable_local_catalog_answers_its_error_and_no_suggestions(app, tmp_path):
    root = build_tree(tmp_path / "sdk")
    use_source(app, root)
    shutil.rmtree(root / "generators" / "second")
    catalog = app.get("/api/suggested").json()
    assert catalog["suggestions"] == []
    assert "generators/second" in catalog["error"]


def test_a_malformed_cell_yaml_is_an_error_not_a_failure(app, tmp_path):
    root = build_tree(tmp_path / "sdk")
    use_source(app, root)
    (root / "generators" / "second" / "cell.yaml").write_text("name: [", encoding="utf-8")
    response = app.get("/api/suggested")
    assert response.status_code == 200, response.text
    assert response.json()["suggestions"] == [] and response.json()["error"]
    created = create_suggested(app, "Broken")
    assert created.status_code == 400 and "Cannot read the suggestion" in created.json()["detail"]


def test_runner_drafts_come_from_both_asset_trees(app, tmp_path):
    """A runner package in a renderer-tree cell is offered like one in a
    generator cell, and a kept draft lands in its generator's cell."""
    from cell_placement import capabilities_stack_cells
    from state import state
    from suggested_trees import runner, write
    root = build_tree(tmp_path / "sdk")
    write(root / "renderers" / "views" / "beside" / "beside.runner", yaml.safe_dump(runner("beside"), sort_keys=False))
    (entry,) = use_source(app, root)["suggestions"]
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

def test_the_bundled_tree_is_the_default_and_reads_without_the_network(app, github, bundled):
    catalog = app.get("/api/suggested").json()
    assert catalog["source"] == {"kind": "bundled", "path": str(bundled), "fetched_at": None}
    assert catalog["error"] is None and _names(catalog) == ["Bundled"]
    created = create_suggested(app, "From the bundle")
    assert created.status_code == 200, created.text
    names = {cell["name"] for stack in created.json()["nav_stacks"] + created.json()["main_stacks"]
             for cell in stack["cells"]}
    assert {"First", "Second", "Views", "Tour"} <= names
    assert github.calls == 0


def test_a_checkout_bundles_its_own_content_and_builtins(api_app, github):
    """Without the wheel's package data, the bundled trees are the
    checkout's ``content/`` and ``builtins/``, and the catalog reads the
    first-party suggestions from there."""
    import importlib.util

    import genesis
    from fastapi.testclient import TestClient
    assert importlib.util.find_spec("collimate_content") is None
    assert genesis.bundled_dir("content") == str(REPO / "content")
    assert genesis.bundled_dir("builtins") == str(REPO / "builtins")
    assert (genesis.GENERATORS_DIR, genesis.RENDERERS_DIR, genesis.BUILTIN_DIR) == (
        str(REPO / "content" / "generators"), str(REPO / "content" / "renderers"), str(REPO / "builtins"))
    with TestClient(api_app) as client:
        catalog = client.get("/api/suggested").json()
    assert catalog["source"] == {"kind": "bundled", "path": str(REPO / "content"), "fetched_at": None}
    assert catalog["error"] is None and "standard" in [s["id"] for s in catalog["suggestions"]]
    assert github.calls == 0


def test_an_installed_wheel_bundles_its_package_data(tmp_path, monkeypatch):
    """With the ``collimate`` wheel's ``collimate_content`` and
    ``collimate_builtins`` importable (namespace packages, as the wheel
    installs them), they are the bundled trees, whatever checkout is near."""
    import genesis
    site = tmp_path / "site-packages"
    for folder in ("collimate_content/generators", "collimate_content/renderers",
                   "collimate_content/suggested", "collimate_builtins/text"):
        (site / folder).mkdir(parents=True)
    monkeypatch.syspath_prepend(str(site))
    assert genesis.bundled_dir("content") == str(site / "collimate_content")
    assert genesis.bundled_dir("builtins") == str(site / "collimate_builtins")
    assert genesis._resolve_assets_dir("renderers") == str(site / "collimate_content" / "renderers")
    assert genesis._resolve_assets_dir("builtin") == str(site / "collimate_builtins")


# ---------------------------------------------------------------------------
# Refresh: the latest release's content.zip
# ---------------------------------------------------------------------------

def test_a_refresh_fetches_the_release_content_and_swaps_the_pointer(app, tmp_path, github):
    import suggested
    github.body = _content_zip(_named(build_tree(tmp_path / "release"), "Released"))
    catalog = app.get("/api/suggested", params={"refresh": 1}).json()
    assert catalog["error"] is None
    assert catalog["source"]["kind"] == "github" and catalog["source"]["path"] == suggested.GITHUB_CONTENT_ZIP
    assert catalog["source"]["fetched_at"]
    assert _names(catalog) == ["Released"]
    (first,) = _trees()
    assert (Path(suggested.CACHE_DIR) / first / "suggested" / "basic.json").is_file()

    # refresh=0 serves the fetched tree without fetching.
    assert app.get("/api/suggested").json() == catalog and github.calls == 1
    app.get("/api/suggested", params={"refresh": 1})
    second = next(name for name in _trees() if name != first)
    assert _trees() == sorted([first, second])
    app.get("/api/suggested", params={"refresh": 1})
    assert first not in _trees() and second in _trees() and len(_trees()) == 2
    assert not [p for p in Path(suggested.CACHE_DIR).iterdir() if p.suffix in (".zip", ".json") and p.name != "github.json"]


def test_a_pointer_another_app_version_fetched_is_ignored_until_the_next_refresh(app, tmp_path, github, bundled):
    """The pointer records the app version that fetched it. After an
    upgrade (a pointer from another version) the bundled content is read,
    for the catalog and for a new channel's provenance, until the user
    refreshes again."""
    import suggested
    from app_version import app_version
    from suggested_trees import provenance
    github.body = _content_zip(_named(build_tree(tmp_path / "release"), "Released"))
    assert _names(app.get("/api/suggested", params={"refresh": 1}).json()) == ["Released"]
    pointer = json.loads(Path(suggested.POINTER).read_text(encoding="utf-8"))
    assert pointer["app_version"] == app_version()

    Path(suggested.POINTER).write_text(json.dumps({**pointer, "app_version": "0.0.0+older"}), encoding="utf-8")
    catalog = app.get("/api/suggested").json()
    assert (catalog["source"]["kind"], _names(catalog)) == ("bundled", ["Bundled"])
    created = create_suggested(app, "After upgrade", tutorial=False)
    assert created.status_code == 200, created.text
    cell = created.json()["nav_stacks"][0]["cells"][-1]
    assert {(r["source_kind"], r["source_id"]) for r in provenance(created.json()["id"], cell["id"])} == {
        ("bundled", str(bundled))}

    refreshed = app.get("/api/suggested", params={"refresh": 1}).json()
    assert (refreshed["source"]["kind"], _names(refreshed)) == ("github", ["Released"])
    assert json.loads(Path(suggested.POINTER).read_text(encoding="utf-8"))["app_version"] == app_version()
    assert github.calls == 2


def test_the_next_channel_starts_from_the_fetched_content(app, tmp_path, github):
    release = build_tree(tmp_path / "release")
    write = release / "generators" / "first" / "cell.yaml"
    write.write_text(yaml.safe_dump({"name": "Released first", "order": 1}), encoding="utf-8")
    suggestion = release / "suggested" / "basic.json"
    data = json.loads(suggestion.read_text(encoding="utf-8"))
    data["generators"][0][0] = "Released first"
    suggestion.write_text(json.dumps(data), encoding="utf-8")
    before = create_suggested(app, "Before", tutorial=False)
    github.body = _content_zip(release)
    app.get("/api/suggested", params={"refresh": 1})
    after = create_suggested(app, "After", tutorial=False)
    assert before.status_code == after.status_code == 200, (before.text, after.text)

    def names(response):
        return {cell["name"] for stack in response.json()["nav_stacks"] for cell in stack["cells"]}
    assert "First" in names(before) and "Released first" not in names(before)
    assert "Released first" in names(after) and "First" not in names(after)
    assert github.calls == 1


def test_a_failed_refresh_keeps_the_fetched_copy_beside_the_error(app, tmp_path, github):
    github.body = _content_zip(_named(build_tree(tmp_path / "release"), "Released"))
    fetched = app.get("/api/suggested", params={"refresh": 1}).json()
    github.status = 502
    kept = app.get("/api/suggested", params={"refresh": 1}).json()
    assert kept["suggestions"] == fetched["suggestions"] and _names(kept) == ["Released"]
    assert kept["source"] == fetched["source"]
    assert kept["error"] == "Cannot fetch the latest release's content from GitHub (HTTP 502)"
    assert len(_trees()) == 1
    calls = github.calls
    created = create_suggested(app, "From the fetched copy")
    assert created.status_code == 200, created.text
    assert github.calls == calls


def test_a_failed_first_refresh_answers_the_bundled_tree_with_the_error(app, github, bundled):
    github.status = 404
    catalog = app.get("/api/suggested", params={"refresh": 1}).json()
    assert catalog["source"] == {"kind": "bundled", "path": str(bundled), "fetched_at": None}
    assert _names(catalog) == ["Bundled"]
    assert catalog["error"] == "Cannot fetch the latest release's content from GitHub (HTTP 404)"
    assert _trees() == []
    created = create_suggested(app, "Offline")
    assert created.status_code == 200, created.text
    assert app.get("/api/suggested").json()["error"] is None and github.calls == 1


@pytest.mark.parametrize(("failure", "reason"), [
    (httpx.ConnectTimeout("handshake took too long, see https://developer.mozilla.org/"), "timed out"),
    (httpx.ConnectError("[Errno -2] Name or service not known"), "connection failed"),
])
def test_a_transport_failure_answers_a_short_reason(app, monkeypatch, failure, reason):
    import suggested

    def fail(request):
        raise failure

    monkeypatch.setattr(suggested, "_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(fail)))
    catalog = app.get("/api/suggested", params={"refresh": 1}).json()
    assert catalog["error"] == f"Cannot fetch the latest release's content from GitHub ({reason})"
    assert catalog["source"]["kind"] == "bundled" and _names(catalog) == ["Bundled"]


def test_an_interrupted_fetch_leaves_nothing_behind(app, tmp_path, github, monkeypatch):
    """A fetch that dies mid-download removes its partial archive; leftovers
    of a killed process (a partial archive and a staged pointer) are swept
    by the next fetch."""
    import suggested
    cache = Path(suggested.CACHE_DIR)
    github.body = _content_zip(build_tree(tmp_path / "release"))
    real = suggested._download

    def dies(target):
        target.write(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(suggested, "_download", dies)
    with pytest.raises(KeyboardInterrupt):
        suggested.fetch_github()
    assert sorted(p.name for p in cache.iterdir()) == []

    (cache / "partial-dead.zip").write_bytes(b"killed mid-download")
    (cache / "partial-dead.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(suggested, "_download", real)
    assert app.get("/api/suggested", params={"refresh": 1}).json()["error"] is None
    assert sorted(p.name for p in cache.iterdir()) == sorted(["github.json", *_trees()]) and len(_trees()) == 1


@pytest.mark.parametrize(("cap", "reason"), [
    ("MAX_DOWNLOAD_BYTES", "archive larger than 3 bytes"),
    ("MAX_MEMBERS", "archive holds more than 3 members"),
    ("MAX_MEMBER_BYTES", "archive member larger than 3 bytes"),
    ("MAX_EXTRACTED_BYTES", "archive expands past 3 bytes"),
])
def test_archive_caps_refuse_the_fetch(app, tmp_path, github, monkeypatch, cap, reason):
    import suggested
    github.body = _content_zip(build_tree(tmp_path / "release"))
    monkeypatch.setattr(suggested, cap, 3)
    catalog = app.get("/api/suggested", params={"refresh": 1}).json()
    assert catalog["error"] == f"Cannot fetch the latest release's content from GitHub ({reason})"
    assert catalog["source"]["kind"] == "bundled" and _names(catalog) == ["Bundled"]
    assert sorted(p.name for p in Path(suggested.CACHE_DIR).iterdir()) == []


@pytest.mark.parametrize("shape", ["wrapped", "missing", "extra"])
def test_the_zip_root_holds_the_three_content_folders_only(app, tmp_path, github, shape):
    import suggested
    tree = build_tree(tmp_path / "release")
    if shape == "missing":
        shutil.rmtree(tree / "renderers")
    github.body = _content_zip(tree, prefix="content/" if shape == "wrapped" else "",
                               extra={"README.md": b"extra"} if shape == "extra" else None)
    catalog = app.get("/api/suggested", params={"refresh": 1}).json()
    assert catalog["error"] == ("Cannot fetch the latest release's content from GitHub "
                                "(archive root must hold generators/, renderers/ and suggested/ only)")
    assert sorted(p.name for p in Path(suggested.CACHE_DIR).iterdir()) == []


def _symlink(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info


@pytest.mark.parametrize("member", [
    "/generators/absolute.txt", "generators/../../escape.txt", "generators\\escape.txt", "symlink",
])
def test_an_unsafe_member_refuses_the_fetch(app, tmp_path, github, member):
    import suggested
    buffer = io.BytesIO(_content_zip(build_tree(tmp_path / "release")))
    with zipfile.ZipFile(buffer, "a") as bundle:
        if member == "symlink":
            bundle.writestr(_symlink("generators/link"), "../../../escape.txt")
        else:
            bundle.writestr(member, b"no")
    github.body = buffer.getvalue()
    catalog = app.get("/api/suggested", params={"refresh": 1}).json()
    assert catalog["error"] == "Cannot fetch the latest release's content from GitHub (archive holds an unsafe member)"
    assert catalog["source"]["kind"] == "bundled"
    assert sorted(p.name for p in Path(suggested.CACHE_DIR).iterdir()) == []
    assert not (Path(suggested.CACHE_DIR).parent / "escape.txt").exists()


def test_a_body_that_is_no_zip_refuses_the_fetch(app, github):
    github.body = b"<html>not a release asset</html>"
    catalog = app.get("/api/suggested", params={"refresh": 1}).json()
    assert catalog["error"] == "Cannot fetch the latest release's content from GitHub (the archive could not be unpacked)"
    assert _trees() == []


def test_a_pointer_at_anything_but_a_content_tree_is_no_cache(app, github):
    """A ``github.json`` whose tree holds no ``suggested/`` folder (an
    unpacked repository archive, say) is passed over for the bundled tree."""
    import suggested
    from app_version import app_version
    cache = Path(suggested.CACHE_DIR)
    (cache / "github-old" / "collimate-main" / "content" / "suggested").mkdir(parents=True)
    (cache / "github.json").write_text(json.dumps({"dir": "github-old", "fetched_at": "2026-01-01T00:00:00+00:00",
                                                   "app_version": app_version()}), encoding="utf-8")
    catalog = app.get("/api/suggested").json()
    assert catalog["source"]["kind"] == "bundled" and catalog["error"] is None
