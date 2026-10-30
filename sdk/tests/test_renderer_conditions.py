# SPDX-License-Identifier: MIT
"""Regression coverage for the schema_form renderer's condition evaluator
against the cross-language contract fixture
``shared/set-yaml-conditions-contract.json``.

The evaluator is exported from the renderer package entrypoint
``renderers/utility/schema_form/main.tsx``. To exercise it
under plain Node, this test first bundles main.tsx with its CDN/SDK imports
stubbed (``scripts/bundle_renderer_test.mjs``) into a temporary importable ESM
module, then runs ``conditions.test.ts`` under node:test via the tsx loader with
``SCHEMA_FORM_BUNDLE`` pointing at that bundle — see ``conditions.test.ts``.
Skips when Node, esbuild, or the tsx loader isn't available (e.g. a Python-only
CI job that never ran ``npm --prefix scripts install``); uses ``tests/_node_renderer.py``.
"""
from __future__ import annotations


from tests._node_renderer import (
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import RENDERERS

_TEST_FILE = RENDERERS / "utility" / "schema_form" / "conditions.test.ts"
_MAIN_TSX = RENDERERS / "utility" / "schema_form" / "main.tsx"

pytestmark = node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the renderer conditions test')


def test_renderer_conditions_match_contract__F1817(tmp_path):
    run_node_renderer(_MAIN_TSX, tmp_path / 'schema_form_bundle.mjs', _TEST_FILE, 'SCHEMA_FORM_BUNDLE',
                      label='renderer conditions test', bundle_label='main.tsx')


def test_schema_form_settings_claim__D13():
    import yaml

    definition = yaml.safe_load((_MAIN_TSX.parent / "schema_form.renderer").read_text())
    assert definition["extensions"] == [".set_yaml"]
    assert definition["entrypoint"] == "main.tsx"
