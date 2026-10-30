# SPDX-License-Identifier: MIT
"""The run card renderer's pure model under node:test: the ``.run`` card the
host writes for every run (parse, title, start time) and the line that says
where the run's Stop and log are, and (R03) the card's
styles on the skin tokens with no palette or colour literal — see
``builtin/live/run/run.test.ts``.

The renderer is a single self-contained module, so this shim first bundles
``main.tsx`` with its CDN/SDK imports stubbed
(``scripts/bundle_renderer_test.mjs``) and hands the bundle path to the node
test via ``RUN_RENDERER_BUNDLE`` — the shared
``tests/_node_renderer.py`` harness. ``TZ=UTC`` pins the local-time
formatting. Skips when Node, esbuild, or the tsx loader isn't installed
(``npm --prefix scripts install``)."""
from __future__ import annotations


from tests._node_renderer import (
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import BUILTINS

_TEST_FILE = BUILTINS / "live" / "run" / "run.test.ts"
_MAIN_TSX = BUILTINS / "live" / "run" / "main.tsx"

pytestmark = node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the run renderer model test')


def test_run_renderer_model(tmp_path):
    run_node_renderer(_MAIN_TSX, tmp_path / 'run_bundle.mjs', _TEST_FILE, 'RUN_RENDERER_BUNDLE',
                      label='run renderer model test', bundle_label='main.tsx', env={'TZ': 'UTC'})
