# SPDX-License-Identifier: MIT
"""Tree-driven cell layout: parent folders are the cells (scan_cells +
cell.yaml metadata), and read_asset_cells carries that layout onto each
CellSpec — folder, name, icon, order, extras and the cell.yaml itself."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from collimate._host.packaging import read_asset_cells, scan_cells, shared_dir

from tests._workspace import BUILTINS, GENERATORS, RENDERERS


_REPO = Path(__file__).resolve().parents[1]


def _mk_tree(root: Path) -> None:
    (root / "_shared_lib").mkdir(parents=True)
    (root / "_shared_lib" / "__init__.py").write_text("X = 1\n")
    beta = root / "beta_cell"
    (beta / "gen_one").mkdir(parents=True)
    (beta / "gen_one" / "custom.generator").write_text("id: gen-one\n")
    (beta / "cell.yaml").write_text(yaml.safe_dump({
        "name": "Beta", "icon": "scale",
        "order": 10, "shared": ["_shared_lib"], "extra": {"tier": "core"},
    }))
    alpha = root / "alpha_cell"
    (alpha / "gen_two").mkdir(parents=True)
    (alpha / "gen_two" / "custom.generator").write_text("id: gen-two\n")
    # no cell.yaml → defaults (titled name, order 100)


def test_scan_cells_reads_manifests_and_orders(tmp_path):
    _mk_tree(tmp_path)
    cells = scan_cells(tmp_path)
    assert [c.name for c in cells] == ["Beta", "Alpha Cell"]   # order 10 first, then default
    beta = cells[0]
    assert beta.folder == "beta_cell"
    assert beta.icon == "scale" and beta.order == 10
    assert beta.extra == {"tier": "core"}
    # Underscore dirs are libraries, not cells.
    assert all(c.folder != "_shared_lib" for c in cells)


def test_shared_dir_resolves_only_underscore_top_level(tmp_path):
    _mk_tree(tmp_path)
    assert shared_dir(tmp_path, "_shared_lib") is not None
    assert shared_dir(tmp_path, "beta_cell") is None            # not underscore
    assert shared_dir(tmp_path, "../_shared_lib") is None       # no traversal
    assert shared_dir(tmp_path, "_missing") is None


def test_read_asset_cells_carries_the_layout(tmp_path):
    src = tmp_path / "generators"
    _mk_tree(src)
    cells = read_asset_cells(src, "generator")
    assert [c.name for c in cells] == ["Beta", "Alpha Cell"]
    beta = cells[0]
    assert beta.folder == "beta_cell" and beta.icon == "scale"
    assert beta.order == 10 and beta.extra == {"tier": "core"}
    # The cell.yaml travels in the cell; the shared library it names does
    # not: admission resolves it from the installed helper set.
    names = {f.name for f in beta.files}
    assert names == {"cell.yaml", "gen_one/custom.generator"}


def test_repo_trees_scan_to_the_expected_cells():
    # The shipped generator cells in display order: Web, Git, Launch and the
    # Agent (judgment/guide/builder/agent_run folded into agent runners).
    gens = scan_cells(GENERATORS)
    assert [c.name for c in gens] == ["Web", "Git", "Launch", "Agent"]
    rends = scan_cells(RENDERERS)
    assert [c.name for c in rends] == ["Documents", "Visual", "Utility"]
    builtin = scan_cells(BUILTINS)
    assert [(c.name, c.folder) for c in builtin] == [
        ("Definitions", "definitions"), ("Live", "live"), ("Text", "text")]
    assert all(c.icon for c in builtin)
    agent = next(c for c in gens if c.name == "Agent")
    assert agent.icon


def test_scan_cells_missing_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_cells(tmp_path / "nope")


def test_scan_cells_skips_interpreter_debris(tmp_path):
    """A folder holding only orphaned __pycache__ trees is not a cell — stale
    caches from a deleted experiment must not change the scanned layout."""
    from collimate._host.packaging.layout import scan_cells

    real = tmp_path / "real_cell" / "gen"
    real.mkdir(parents=True)
    (real / "custom.generator").write_text("id: g\n")
    debris = tmp_path / "debris" / "old" / "__pycache__"
    debris.mkdir(parents=True)
    (debris / "old.cpython-313.pyc").write_bytes(b"\x00")

    assert [c.folder for c in scan_cells(tmp_path)] == ["real_cell"]


def test_scan_cells_keeps_explicit_order_zero(tmp_path):
    """``order: 0`` is a real sort key, not "missing" — it sorts first."""
    _mk_tree(tmp_path)
    (tmp_path / "alpha_cell" / "cell.yaml").write_text(yaml.safe_dump({"order": 0}))
    cells = scan_cells(tmp_path)
    assert [c.folder for c in cells] == ["alpha_cell", "beta_cell"]
    assert cells[0].order == 0
