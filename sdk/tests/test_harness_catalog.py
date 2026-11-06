# SPDX-License-Identifier: MIT
"""The harness adapters' projections are generated, byte for byte: the core
catalog, the settings schema's harness options, the manifest's harness list
and presets (``scripts/sync_harness_catalog.py``)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from tests._workspace import GENERATORS


ROOT = Path(__file__).resolve().parents[1]


def _sync():
    spec = importlib.util.spec_from_file_location("sync_harness_catalog", ROOT / "scripts" / "sync_harness_catalog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_projection_equals_what_the_adapters_generate():
    sync = _sync()
    stale = [str(path.relative_to(sync.WORKSPACE)) for path, text in sync.expected().items()
             if not path.is_file() or path.read_text(encoding="utf-8") != text]
    assert stale == [], "run python scripts/sync_harness_catalog.py: " + ", ".join(stale)


def test_the_catalog_is_the_registry_as_core_sees_it():
    from _agent_shared.harnesses import HARNESS
    from collimate.harness_catalog import HARNESSES
    assert list(HARNESSES) == list(HARNESS)
    for name, row in HARNESS.items():
        facts = HARNESSES[name]
        assert facts["locus"] == row.locus and facts["providers"] == row.providers
        assert facts["auths"] == row.auths and facts["terminal"] == (row.terminal.tier if row.terminal else "")
        assert facts["host_twin"] == row.host_twin and facts["cell_mcp"] == row.tools.cell_mcp


def test_the_settings_schema_offers_exactly_the_roster():
    from _agent_shared.harnesses import HARNESS
    fields = {f["key"]: f for f in yaml.safe_load(
        (GENERATORS / "agent/agent/agent-config.set_yaml").read_text())["fields"]}
    options = [o["value"] for o in fields["backend"]["options"]]
    assert options == list(HARNESS)
    no_mcp = sorted(name for name, row in HARNESS.items() if not row.tools.cell_mcp)
    for option in fields["mcp"]["options"]:
        if isinstance(option, dict) and "enable_if" in option:
            assert sorted(option["enable_if"]["not"]["backend"]) == no_mcp
    manifest = yaml.safe_load((GENERATORS / "agent/agent/agent.generator").read_text())
    assert manifest["account_requirements"]["model"]["harnesses"] == list(HARNESS)


def test_a_check_reports_a_hand_edit(tmp_path, monkeypatch):
    sync = _sync()
    target = tmp_path / "harness_catalog.py"
    target.write_text("HARNESSES = {}\n")
    monkeypatch.setattr(sync, "CATALOG", target)
    assert sync.main(["--check"]) == 1
