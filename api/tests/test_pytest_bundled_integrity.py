"""Coverage for bundled_integrity: the bundled trees against the installed
``collimate`` wheel's RECORD, which /ready reports as ``checks.bundled``.

Each case installs a fixture distribution (a dist-info with a RECORD and the
three package-data trees) on ``sys.path``, so ``importlib.metadata`` and
``genesis.bundled_dir`` resolve it as they resolve an installed wheel."""
from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.metadata
from pathlib import Path

import pytest

TREES = {
    "collimate_content/generators/cell/entry.py": b"print(1)\n",
    "collimate_content/renderers/cell/view/view.renderer": b"id: view\n",
    "collimate_content/suggested/standard.json": b"{}\n",
    "collimate_builtins/text/text/text.renderer": b"id: text\n",
    "collimate_module_snapshot/index.json": b"{}\n",
    "collimate_module_snapshot/modules/" + "a" * 64: b"export {};\n",
}


def _digest(body: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode()


def _install(site: Path, files: dict[str, bytes], record: dict[str, bytes] | None = None) -> Path:
    """Write ``files`` under ``site`` with a collimate dist-info whose RECORD
    lists ``record`` (default: ``files``) with their hashes."""
    for name, body in files.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    info = site / "collimate-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.4\nName: collimate\nVersion: 0.1.0\n")
    rows = [f"{name},{_digest(body)},{len(body)}" for name, body in (files if record is None else record).items()]
    # An installer's compiled bytecode is recorded without a hash.
    rows += ["collimate_content/generators/cell/__pycache__/entry.cpython-313.pyc,,",
             "collimate-0.1.0.dist-info/METADATA,,", "collimate-0.1.0.dist-info/RECORD,,"]
    (info / "RECORD").write_text("\n".join(rows) + "\n")
    return site


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Install a fixture wheel's trees ahead of the checkout and answer its
    site directory; ``distribution("collimate")`` reads its dist-info."""
    def install(files=TREES, record=None):
        site = _install(tmp_path / "site", files, record)
        monkeypatch.syspath_prepend(str(site))
        importlib.invalidate_caches()
        monkeypatch.setattr(importlib.metadata, "distribution",
                            lambda name: importlib.metadata.PathDistribution(site / "collimate-0.1.0.dist-info"))
        return site
    return install


def test_a_checkout_reads_its_live_trees_and_reports_checkout():
    import bundled_integrity
    result = bundled_integrity.verify_bundled()
    assert result["status"] == "checkout" and result["checked"] == 0


def test_installed_trees_matching_their_record_are_ok(installed):
    import bundled_integrity
    import genesis
    site = installed()
    assert genesis.bundled_dir("module-snapshot") == str(site / "collimate_module_snapshot")
    (site / "collimate_content/generators/cell/__pycache__").mkdir()
    (site / "collimate_content/generators/cell/__pycache__/entry.cpython-313.pyc").write_bytes(b"bytecode")
    result = bundled_integrity.verify_bundled()
    assert result == {"status": "ok", "checked": len(TREES), "mismatched": [], "missing": [], "extra": []}


def test_an_edited_missing_or_stray_file_is_drift(installed):
    import bundled_integrity
    site = installed()
    (site / "collimate_content/suggested/standard.json").write_bytes(b'{"edited": true}\n')
    (site / "collimate_builtins/text/text/text.renderer").unlink()
    (site / "collimate_module_snapshot/modules/stray").write_bytes(b"stray")
    result = bundled_integrity.verify_bundled()
    assert result["status"] == "drift"
    assert result["mismatched"] == ["collimate_content/suggested/standard.json"]
    assert result["missing"] == ["collimate_builtins/text/text/text.renderer"]
    assert result["extra"] == ["collimate_module_snapshot/modules/stray"]


def test_trees_no_record_lists_report_checkout(installed):
    import bundled_integrity
    installed(record={})
    assert bundled_integrity.verify_bundled()["status"] == "checkout"


def test_readiness_reports_drift_from_the_record(installed, fresh_cwd):
    import main
    site = installed()
    (site / "collimate_module_snapshot/index.json").write_bytes(b"tampered\n")
    main._load_readiness_diagnostics()
    assert main._readiness_diagnostics["bundled"] == "drift: 1 files differ from the collimate RECORD"
