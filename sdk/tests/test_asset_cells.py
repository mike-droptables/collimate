# SPDX-License-Identifier: MIT
"""read_asset_cells: an asset tree read as CellSpecs, the one way a host
seeds and resets shipped cells (plan 1011, row TR-05)."""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

import pytest
import yaml

from collimate._host.packaging import (
    CELL_MANIFEST, DEFAULT_CELL_ORDER, bundled_libraries, library_id, read_asset_cells, read_library, shared_names,
)
from tests._workspace import BUILTINS, GENERATORS, RENDERERS

_NAMESPACE = uuid.UUID("0bda06ef-1a24-4c63-a6a4-3c6b86e85418")


def _write(path: Path, body: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body)


def _tree(root: Path) -> None:
    _write(root / "_lib_a" / "_lib_a.library", "id: _lib_a\nname: Lib A\ndescription: The A helpers.\n")
    _write(root / "_lib_a" / "__init__.py", "A = 1\n")
    _write(root / "_lib_a" / "helpers" / "util.py", "def f():\n    return 1\n")
    _write(root / "_lib_b" / "__init__.py", "B = 1\n")
    _write(root / "zeta" / "cell.yaml", yaml.safe_dump({
        "name": "Zeta Tools", "icon": "wrench", "order": 5, "shared": ["_lib_a"]}))
    _write(root / "zeta" / "tool" / "tool.generator", "id: tool\n")
    _write(root / "alpha" / "thing" / "thing.generator", "id: thing\n")
    _write(root / "mid" / "cell.yaml", yaml.safe_dump({"order": 5}))
    _write(root / "mid" / "piece" / "piece.generator", "id: piece\n")


def _files(cell) -> dict:
    return {f.name: f for f in cell.files}


def test_ids_are_uuid5_of_kind_and_display_name_TR05(tmp_path):
    _tree(tmp_path)
    for kind in ("generator", "renderer"):
        for cell in read_asset_cells(tmp_path, kind):
            assert cell.id == str(uuid.uuid5(_NAMESPACE, f"{kind}-cell-{cell.name}"))
    gens = {c.name: c.id for c in read_asset_cells(tmp_path, "generator")}
    rends = {c.name: c.id for c in read_asset_cells(tmp_path, "renderer")}
    assert gens.keys() == rends.keys()
    assert not set(gens.values()) & set(rends.values())
    assert gens == {c.name: c.id for c in read_asset_cells(tmp_path, "generator")}


def test_cells_come_in_display_order_with_their_metadata_TR05(tmp_path):
    _tree(tmp_path)
    cells = read_asset_cells(str(tmp_path), "generator")
    # order 5 first (ties by folder name), then the default order.
    assert [(c.folder, c.name, c.order) for c in cells] == [
        ("mid", "Mid", 5), ("zeta", "Zeta Tools", 5), ("alpha", "Alpha", DEFAULT_CELL_ORDER)]
    assert [c.icon for c in cells] == ["", "wrench", ""]
    assert all(c.extra == {} for c in cells)


def test_a_cell_keeps_its_cell_yaml_and_leaves_its_shared_libraries_to_the_host_TR05(tmp_path):
    """The reader never resolves ``shared:``: the cell.yaml that declares it
    travels in the cell, and a host resolves each name to a library (on the
    channel's Capabilities, else one it ships), whatever the cell's source."""
    _tree(tmp_path)
    cells = {c.folder: c for c in read_asset_cells(tmp_path, "generator")}
    assert sorted(_files(cells["zeta"])) == ["cell.yaml", "tool/tool.generator"]
    assert yaml.safe_load(_files(cells["zeta"])["cell.yaml"].content)["shared"] == ["_lib_a"]
    assert sorted(_files(cells["alpha"])) == ["thing/thing.generator"]
    assert not any(f.name.startswith("_lib_") for c in cells.values() for f in c.files)
    # Libraries are never cells of their own.
    assert set(cells) == {"alpha", "mid", "zeta"}


def test_a_bundled_library_is_its_folder_holding_a_definition_naming_it(tmp_path):
    """``read_library`` reads ``root/<name>/`` as the library ``name`` when
    it holds ``<name>/<x>.library`` whose id is ``name``; a folder without
    one (``_lib_b``) is no library."""
    _tree(tmp_path)
    _write(tmp_path / "_lib_a" / "__pycache__" / "x.cpython-313.pyc", b"\x00")
    _write(tmp_path / "_lib_a" / "logo.png", b"\x89PNG\x00\xff")
    files = read_library(tmp_path, "_lib_a")
    assert [f.name for f in files] == [
        "_lib_a/__init__.py", "_lib_a/_lib_a.library", "_lib_a/helpers/util.py", "_lib_a/logo.png"]
    assert files[0].content == "A = 1\n" and files[0].language == "python"
    (png,) = [f for f in files if f.name == "_lib_a/logo.png"]
    assert png.data == b"\x89PNG\x00\xff" and png.blob_hash == hashlib.sha256(png.data).hexdigest()
    assert read_library(tmp_path, "_lib_b") is None and read_library(tmp_path, "_missing") is None
    assert read_library(tmp_path, "zeta") is None
    assert bundled_libraries(tmp_path) == ["_lib_a"]


@pytest.mark.parametrize("path, body, identifier", [
    ("_lib/_lib.library", "id: _lib\n", "_lib"),
    ("_lib/other.library", "id: _lib\n", "_lib"),
    ("_lib/_lib.library", "id: _other\n", None),       # the id names another folder
    ("deep/_lib/_lib.library", "id: _lib\n", None),    # not at the top of its cell
    ("lib/lib.library", "id: lib\n", None),            # not an underscore name
    ("_Lib/_Lib.library", "id: _Lib\n", None),
    ("_lib/_lib.library", "[1]\n", None),
    ("_lib/_lib.library", "id: [\n", None),
])
def test_a_library_id_is_the_folder_its_definition_tops(path, body, identifier):
    assert library_id(path, body) == identifier


def test_shared_names_reads_the_cell_yaml_list():
    assert shared_names(None) == [] and shared_names("name: Plain\n") == [] and shared_names("shared: []\n") == []
    assert shared_names(yaml.safe_dump({"shared": ["_lib_b", "_lib_a", "_lib_b"]})) == ["_lib_b", "_lib_a"]


@pytest.mark.parametrize("body, message", [
    ("shared: _lib_a\n", "'shared' must be a list of library names"),
    ("shared: [1]\n", "'shared' must be a list of library names"),
    ("shared: [zeta]\n", "'zeta', which is no library id"),
    ("shared: [../_lib_a]\n", "'../_lib_a', which is no library id"),
    ("shared: [\n", "cell.yaml is not YAML"),
])
def test_shared_names_refuses_a_malformed_list(body, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        shared_names(body)


def test_every_shipped_shared_name_resolves_to_a_shipped_library():
    """The app's libraries are the generators tree's library folders: every
    ``shared:`` a shipped cell declares names one of them."""
    shipped = set(bundled_libraries(GENERATORS))
    assert shipped == {"_env_shared", "_agent_shared", "_web_shared"}
    named = 0
    for directory in (GENERATORS, RENDERERS, BUILTINS):
        for cell in directory.iterdir():
            manifest = cell / CELL_MANIFEST
            if cell.name.startswith("_") or not manifest.is_file():
                continue
            names = shared_names(manifest.read_bytes())
            assert set(names) <= shipped, cell.name
            named += len(names)
    assert named


def test_binary_files_carry_raw_bytes_and_a_blob_hash_text_files_content_TR05(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    loose = b"\x00\xff\xfe loose binary"
    _write(tmp_path / "media" / "gen" / "gen.generator", "id: gen\n")
    _write(tmp_path / "media" / "gen" / "logo.png", png)
    _write(tmp_path / "media" / "blob.bin", loose)
    _write(tmp_path / "media" / "README.md", "# Media\n")

    (cell,) = read_asset_cells(tmp_path, "generator")
    files = _files(cell)
    for name, raw in (("gen/logo.png", png), ("blob.bin", loose)):
        assert files[name].data == raw
        assert files[name].blob_hash == hashlib.sha256(raw).hexdigest()
        assert files[name].content == ""
    text = files["gen/gen.generator"]
    assert text.content == "id: gen\n"
    assert text.data is None and text.blob_hash is None
    assert files["README.md"].language == "markdown"
    assert files["gen/logo.png"].language == "text"


def test_interpreter_debris_and_runs_are_skipped_and_cell_yaml_kept_TR05(tmp_path):
    _write(tmp_path / "work" / "cell.yaml", yaml.safe_dump({"name": "Work"}))
    _write(tmp_path / "work" / "gen" / "gen.generator", "id: gen\n")
    _write(tmp_path / "work" / "gen" / "__init__.py", "")
    _write(tmp_path / "work" / "gen" / "__pycache__" / "mod.cpython-313.pyc", b"\x00")
    _write(tmp_path / "work" / "gen" / "stray.pyc", b"\x00")
    _write(tmp_path / "work" / "__pycache__" / "top.cpython-313.pyc", b"\x00")
    _write(tmp_path / "work" / "_runs" / "run-1" / "out.txt", "result\n")

    (cell,) = read_asset_cells(tmp_path, "generator")
    assert sorted(_files(cell)) == ["cell.yaml", "gen/__init__.py", "gen/gen.generator"]


def test_a_cell_with_no_files_is_left_out_TR05(tmp_path):
    _write(tmp_path / "real" / "gen" / "gen.generator", "id: gen\n")
    _write(tmp_path / "hollow" / "cell.yaml", yaml.safe_dump({"name": "Hollow"}))
    assert [c.folder for c in read_asset_cells(tmp_path, "generator")] == ["real"]


@pytest.mark.parametrize("folders, manifests", [
    # two folders that title to the same display name
    (("web-tools", "web_tools"), ({}, {})),
    # two cell.yaml files that declare the same name
    (("alpha", "beta"), ({"name": "Tools"}, {"name": "Tools"})),
])
def test_two_cells_with_one_display_name_are_refused_TR05_F1279(tmp_path, folders, manifests):
    """A cell's id is derived from its display name, so two cells with one
    name would collide and a host would seed only one of them."""
    for folder, meta in zip(folders, manifests):
        _write(tmp_path / folder / "gen" / "gen.generator", "id: gen\n")
        if meta:
            _write(tmp_path / folder / "cell.yaml", yaml.safe_dump(meta))
    with pytest.raises(ValueError) as err:
        read_asset_cells(tmp_path, "generator")
    assert folders[0] in str(err.value) and folders[1] in str(err.value)


def test_unknown_kind_empty_tree_and_missing_root_are_refused_TR05(tmp_path):
    _write(tmp_path / "cell" / "gen" / "gen.generator", "id: gen\n")
    with pytest.raises(ValueError, match="unknown asset kind"):
        read_asset_cells(tmp_path, "widget")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="zero cells"):
        read_asset_cells(empty, "renderer")
    with pytest.raises(FileNotFoundError):
        read_asset_cells(tmp_path / "missing", "generator")


def test_the_shipped_trees_read_with_unique_ids_TR05():
    for directory, kind in ((GENERATORS, "generator"), (RENDERERS, "renderer"), (BUILTINS, "renderer")):
        cells = read_asset_cells(directory, kind)
        assert cells and all(c.files for c in cells)
        assert len({c.id for c in cells}) == len(cells)
        for cell in cells:
            names = [f.name for f in cell.files]
            assert len(names) == len(set(names)), cell.name
            assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)


def test_cell_names_are_unique_across_generator_and_renderer_trees():
    """Every shipped cell lands on the one Capabilities stack, whose reset
    matches cells by name, so no display name may ship in two trees (the
    built-in cards included)."""
    names = {}
    for directory, kind in ((GENERATORS, "generator"), (RENDERERS, "renderer"), (BUILTINS, "renderer")):
        for cell in read_asset_cells(directory, kind):
            assert cell.name not in names, (cell.name, names.get(cell.name), directory.name)
            names[cell.name] = directory.name
