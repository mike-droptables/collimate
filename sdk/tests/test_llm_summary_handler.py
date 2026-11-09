# SPDX-License-Identifier: MIT
"""The shipped ``.llm_summary`` handler: a metadata-only handler (one type,
shown as markdown, no checks) that the renderer bundle build skips."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests._workspace import RENDERERS


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = RENDERERS / "documents" / "llm-summary"


def test_the_llm_summary_handler_is_valid_metadata_only():
    from collimate.handlers import validate_handler

    data = yaml.safe_load((PACKAGE / "llm-summary.handler").read_text(encoding="utf-8"))
    manifest = validate_handler(data)
    assert manifest["id"] == "llm-summary"
    assert [dict(t) for t in manifest["types"]] == [{"ext": ".llm_summary", "render_as": ".md"}]
    assert manifest.get("derived") is None and manifest["examples"] == []
    assert sorted(p.name for p in PACKAGE.iterdir()) == ["llm-summary.handler"]


def test_the_renderer_bundle_build_skips_a_metadata_only_handler():
    from collimate._host.renderer_bundle import _handler_bundles

    bundles = _handler_bundles(RENDERERS)
    assert not any("llm-summary" in repr(bundle) for bundle in bundles)


def test_a_handler_with_a_derived_line_but_no_folder_is_still_refused(tmp_path):
    from collimate._host.renderer_bundle import _handler_bundles

    cell = tmp_path / "renderers" / "documents"
    (cell / "note").mkdir(parents=True)
    (cell / "cell.yaml").write_text("name: Documents\n", encoding="utf-8")
    (cell / "note" / "note.handler").write_text(
        "id: note\nname: Note\ndescription: x\nguidance: x\n"
        "types: [{ext: .note, render_as: .md}]\nexamples: []\nderived: " + "a" * 64 + "\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="has no matching accepted derived folder"):
        _handler_bundles(tmp_path / "renderers")
