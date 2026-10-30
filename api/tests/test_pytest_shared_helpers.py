"""Shared helpers resolve at admission (``package_import.prepare_packages``)
from the installed helper set, whatever the cell's source.

A cell names its helpers under ``shared:`` in its ``cell.yaml``, which travels
in the cell. Admission copies each named ``_*`` library from the installed
helper set (the bundled content's ``generators/``) into the cell under
``<library>/``, in place of whatever the cell carried there, and refuses a
name the set does not hold. A suggestion's folder, a packages zip, another
channel's Capabilities and the catalog's add all take that one path. The
synthetic helper sets and trees are hand-made (``suggested_trees``); one case
creates the Standard suggestion from the bundled content and compares it with
the tree read the old way, where the reader copied the helpers in itself.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from import_cells import upload
from suggested_trees import MAIN, build_tree, create_suggested, folder_hash, install_helpers, provenance, use_source, write

INSTALLED = {"_shared": {"util.py": "INSTALLED = 1\n", "nested/deep.py": "DEEP = 1\n"},
             "_other": {"__init__.py": "OTHER = 1\n"}}
INSTALLED_FILES = {"_shared/util.py": b"INSTALLED = 1\n", "_shared/nested/deep.py": b"DEEP = 1\n"}


@pytest.fixture
def app(api_app):
    from fastapi.testclient import TestClient
    with TestClient(api_app) as client:
        yield client


@pytest.fixture
def installed(app, tmp_path, monkeypatch):
    """The synthetic installed helper set (``INSTALLED``); answers its root."""
    return install_helpers(monkeypatch, INSTALLED, tmp_path / "installed")


def _declaring_tree(root: Path, shared: list[str]) -> Path:
    """The synthetic tree with ``First`` naming ``shared`` and the tree's own
    ``_shared``, which admission never reads."""
    build_tree(root)
    write(root / "generators" / "first" / "cell.yaml",
          yaml.safe_dump({"name": "First", "icon": "scale", "order": 1, "shared": shared}))
    write(root / "generators" / "_shared" / "util.py", "TREE = 1\n")
    return root


def _channel(channel_id: str):
    from state import require_channel
    return require_channel(channel_id)


def _cells(channel_id: str) -> dict:
    from cell_placement import capabilities_stack_cells
    return {cell.name: cell for cell in capabilities_stack_cells(_channel(channel_id))}


def _bodies(cell) -> dict[str, bytes]:
    from cas import read_blob
    return {f.name: read_blob(f.blob_hash) if f.blob_hash else f.content.encode("utf-8") for f in cell.files}


def _helpers(bodies: dict[str, bytes]) -> dict[str, bytes]:
    return {path: body for path, body in bodies.items() if path.startswith("_")}


def _generator(gid: str) -> dict[str, bytes]:
    """A third party's generator package, folder-relative."""
    return {f"{gid}.generator": yaml.safe_dump({
        "id": gid, "name": gid.title(), "summary": "Third-party generator", "entrypoint": f"{gid}.py",
        "main_config": MAIN}).encode(),
        f"{gid}.py": b"from _shared import util\n",
        MAIN: b"fields:\n  - key: message\n    type: text\n    default: hello\n"}


def _zip_cell(client, name: str, files: dict[str, bytes]) -> dict:
    """A packages zip's cell, its bodies stored as the import stores them."""
    return {"name": name, "files": [{"path": path, "blob_hash": upload(client, body)["blob_hash"]}
                                    for path, body in files.items()]}


def _create_from_zip(client, cells: list[dict], name: str = "Zipped"):
    return client.post("/api/channels", params={"name": name}, json={"config": {"packages": {"cells": cells}}})


# ---------------------------------------------------------------------------
# Every source
# ---------------------------------------------------------------------------

def test_a_suggested_cell_gets_the_installed_helpers_its_cell_yaml_names(app, installed, tmp_path):
    root = _declaring_tree(tmp_path / "sdk", ["_shared"])
    use_source(app, root)
    response = create_suggested(app, "Seeded", tutorial=False)
    assert response.status_code == 200, response.text
    cells = _cells(response.json()["id"])
    first = _bodies(cells["First"])
    # The installed copy, never the tree's own; the cell.yaml stays with the cell.
    assert _helpers(first) == INSTALLED_FILES
    assert first["cell.yaml"] == (root / "generators" / "first" / "cell.yaml").read_bytes()
    # A helper is no package: the record names the generator, hashed from its
    # tree folder.
    records = provenance(response.json()["id"], cells["First"].id)
    assert [(r["folder"], r["content_hash"]) for r in records] == [
        ("alpha", folder_hash(root / "generators" / "first" / "alpha"))]
    # A cell that names no helper gets none.
    assert _helpers(_bodies(cells["Second"])) == {}


def test_a_zipped_third_party_generator_gets_the_helper_the_shipped_cells_get(app, installed):
    """The installed copy replaces the zip's whole folder of a named helper;
    a ``_*`` folder the cell.yaml does not name is the cell's own."""
    files = {"cell.yaml": b"shared: [_shared]\n",
             **{f"mine/{path}": body for path, body in _generator("mine").items()},
             "_shared/util.py": b"STALE = 1\n", "_shared/stray.py": b"STRAY = 1\n",
             "_private/notes.py": b"MINE = 1\n"}
    response = _create_from_zip(app, [_zip_cell(app, "Mine", files)])
    assert response.status_code == 200, response.text
    assert response.json()["package_skips"] == []
    (cell,) = _cells(response.json()["id"]).values()
    bodies = _bodies(cell)
    assert _helpers(bodies) == {**INSTALLED_FILES, "_private/notes.py": b"MINE = 1\n"}
    assert bodies["cell.yaml"] == b"shared: [_shared]\n"
    from collimate._host.package_hash import content_hash
    (record,) = provenance(response.json()["id"], cell.id)
    assert (record["source_kind"], record["folder"], record["package_id"]) == ("zip", "mine", "mine")
    assert record["content_hash"] == content_hash(_generator("mine"))


def test_a_helper_the_installed_set_does_not_hold_refuses_the_cell(app, installed, tmp_path):
    before = app.get("/api/channels").json()["channels"]
    files = {"cell.yaml": b"shared: [_shared, _nope]\n",
             **{f"mine/{path}": body for path, body in _generator("mine").items()}}
    response = _create_from_zip(app, [_zip_cell(app, "Mine", files)])
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "Cell 'Mine': cell.yaml names the shared helper '_nope', which the installed helper set "
        "does not hold (it holds _other, _shared)")
    # The suggested path refuses alike, and neither leaves a channel behind.
    use_source(app, _declaring_tree(tmp_path / "sdk", ["_missing"]))
    response = create_suggested(app, "Seeded", tutorial=False)
    assert response.status_code == 400, response.text
    assert "Cell 'First': cell.yaml names the shared helper '_missing'" in response.json()["detail"]
    assert app.get("/api/channels").json()["channels"] == before
    assert len(list(Path(".collimate/channels").glob("*.db"))) == len(before)


def test_a_cell_copied_from_another_channel_resolves_against_the_current_set(app, installed, tmp_path):
    """The copy's helpers come from the set installed now; the source
    channel's cells are untouched."""
    use_source(app, _declaring_tree(tmp_path / "sdk", ["_shared"]))
    source = create_suggested(app, "Source", tutorial=False)
    assert source.status_code == 200, source.text
    write(installed / "generators" / "_shared" / "util.py", "NEWER = 1\n")
    copy = app.post("/api/channels", params={"name": "Copy"},
                    json={"config": {"packages": {"channel_id": source.json()["id"]}}})
    assert copy.status_code == 200, copy.text
    assert _helpers(_bodies(_cells(copy.json()["id"])["First"])) == {
        **INSTALLED_FILES, "_shared/util.py": b"NEWER = 1\n"}
    assert _helpers(_bodies(_cells(source.json()["id"])["First"])) == INSTALLED_FILES


def test_the_catalog_add_resolves_the_helpers_too(app, installed, tmp_path):
    use_source(app, _declaring_tree(tmp_path / "sdk", ["_shared", "_other"]))
    host = app.post("/api/channels", params={"name": "Host"})
    assert host.status_code == 200, host.text
    added = app.post(f"/api/channels/{host.json()['id']}/capabilities/add",
                     json={"suggestion_id": "basic", "folder": "generators/first"})
    assert added.status_code == 200, added.text
    assert _helpers(_bodies(_cells(host.json()["id"])["First"])) == {
        **INSTALLED_FILES, "_other/__init__.py": b"OTHER = 1\n"}


# ---------------------------------------------------------------------------
# The bundled Standard suggestion, as the tree reader used to give it
# ---------------------------------------------------------------------------

def _tree_files(folder: Path) -> dict[str, bytes]:
    """Every file under ``folder`` as the old reader read it: interpreter
    debris and top-level run folders left out."""
    out = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if path.is_file() and "__pycache__" not in rel.parts and path.suffix != ".pyc" and rel.parts[0] != "_runs":
            out[rel.as_posix()] = path.read_bytes()
    return out


def _old_way(folder: Path, generators: Path) -> dict[str, bytes]:
    """The cell the tree reader gave before admission resolved helpers: its
    own files without ``cell.yaml``, then each named library under its name
    read from the tree's generators, runner packages left out (a channel
    created without drafts seeds none)."""
    from collimate.definitions import discover_definitions
    own = _tree_files(folder)
    manifest = yaml.safe_load(own.pop("cell.yaml", b"") or b"{}") or {}
    for name in manifest.get("shared") or []:
        own.update({f"{name}/{rel}": body for rel, body in _tree_files(generators / name).items()})
    runners = [p.prefix + "/" for p in discover_definitions(own).packages if p.kind == "runner"]
    return {path: body for path, body in own.items() if not path.startswith(tuple(runners))}


def test_the_standard_channel_holds_what_the_tree_reader_gave_and_its_cell_yaml(app):
    import genesis
    from collimate._host.package_hash import content_hash
    from collimate._host.packaging import read_suggested_cell, read_suggestions
    content = Path(genesis.bundled_dir("content"))
    (standard,) = [s for s in read_suggestions(content) if s.id == "standard"]
    response = create_suggested(app, "Standard", "standard", tutorial=False)
    assert response.status_code == 200, response.text
    channel_id = response.json()["id"]
    cells = _cells(channel_id)
    refs = [*(("generator", ref) for ref in standard.generators), *(("renderer", ref) for ref in standard.renderers)]
    assert list(cells) == [ref.name for _kind, ref in refs]
    helpers = 0
    for kind, ref in refs:
        folder = content / ref.folder
        bodies = _bodies(cells[ref.name])
        manifest = folder / "cell.yaml"
        if manifest.is_file():
            assert bodies.pop("cell.yaml") == manifest.read_bytes()
        expected = _old_way(folder, content / "generators")
        assert {p: hashlib.sha256(b).hexdigest() for p, b in bodies.items()} == {
            p: hashlib.sha256(b).hexdigest() for p, b in expected.items()}, ref.name
        helpers += len(_helpers(expected))
        # Each file keeps the language the reader gives it.
        spec = read_suggested_cell(content, ref, kind)
        languages = {f.name: f.language for f in spec.files}
        for name, files in _shared_specs(spec).items():
            languages.update({f.name: f.language for f in files})
        assert {f.name: f.language for f in cells[ref.name].files} == {
            name: languages[name] for name in [*bodies, *(["cell.yaml"] if manifest.is_file() else [])]}
        # Every package is recorded with the hash of its tree folder.
        for record in provenance(channel_id, cells[ref.name].id):
            assert record["content_hash"] == content_hash(_tree_files(folder / record["folder"])), (
                ref.name, record["folder"])
    assert helpers, "the Standard suggestion names no shared helper; this case checks nothing"


def _shared_specs(spec) -> dict:
    import genesis
    from collimate._host.packaging import CELL_MANIFEST, shared_libraries
    manifest = next((f for f in spec.files if f.name == CELL_MANIFEST), None)
    if manifest is None:
        return {}
    return shared_libraries(manifest.content, Path(genesis.bundled_dir("content")) / "generators")
