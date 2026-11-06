# SPDX-License-Identifier: MIT
"""Actual framework-free assistant model helpers, bundled for Node regression tests.
Sibling modules compile through the canonical renderer pipeline; this test drives
its pure transcript helpers without React/CDN providers.
"""
from __future__ import annotations


from tests._node_renderer import (
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import BUILTINS

_TEST_FILE = BUILTINS / "live" / "assistant" / "transcript.test.ts"
_MODEL = BUILTINS / "live" / "assistant" / "message-model.ts"
_THEME_TEST = BUILTINS / "live" / "assistant" / "theme.test.ts"
_MAIN = BUILTINS / "live" / "assistant" / "main.tsx"

pytestmark = node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the assistant transcript test')


def test_assistant_transcript_roundtrip(tmp_path):
    run_node_renderer(_MODEL, tmp_path / 'assistant_bundle.mjs', _TEST_FILE, 'ASSISTANT_BUNDLE',
                      label='assistant transcript test', bundle_label='assistant model')


def test_assistant_chat_paints_on_skin_tokens_R03(tmp_path):
    run_node_renderer(_MAIN, tmp_path / 'assistant_main_bundle.mjs', _THEME_TEST, 'ASSISTANT_MAIN_BUNDLE',
                      label='assistant theme test', bundle_label='assistant main.tsx')
