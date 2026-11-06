# SPDX-License-Identifier: MIT
"""The website renderer's frame rule (F1739): the frame keeps its own origin
(``allow-same-origin``) only for another site or for a ``/proxy/`` address on
the app server, the path the server answers solely for a live run's own port.
Any other address on the app server is framed without it.

``frameKeepsOrigin`` is inlined and exported from the renderer's single-file
entrypoint ``builtin/live/website/main.tsx``. To exercise it under plain
Node this test bundles main.tsx with its CDN/SDK imports stubbed
(``scripts/bundle_renderer_test.mjs``), then runs ``frame.test.ts`` under
node:test via the tsx loader with ``WEBSITE_BUNDLE`` pointing at that bundle.
Skips when Node, esbuild or the tsx loader is not available.
"""
from __future__ import annotations


from tests._node_renderer import (
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import BUILTINS

_TEST_FILE = BUILTINS / "live" / "website" / "frame.test.ts"
_MAIN_TSX = BUILTINS / "live" / "website" / "main.tsx"

pytestmark = node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the website frame test')


def test_website_frame_keeps_its_origin_only_for_another_site_or_a_live_proxy__F1739(tmp_path):
    run_node_renderer(_MAIN_TSX, tmp_path / 'website_bundle.mjs', _TEST_FILE, 'WEBSITE_BUNDLE',
                      label='website frame test', bundle_label='main.tsx')
