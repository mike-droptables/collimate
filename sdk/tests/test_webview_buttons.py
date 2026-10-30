# SPDX-License-Identifier: MIT
"""Webview artifact toolbar buttons use the shared pointer vocabulary."""
from pathlib import Path
import subprocess

from tests._workspace import BUILTINS


def test_webview_parser_reads_artifact_buttons__F1765(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "webview.mjs"
    build = subprocess.run([
        "node", str(repo / "scripts/bundle_renderer_test.mjs"),
        str(BUILTINS / "live/webview/main.tsx"), str(bundle),
    ], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    script = tmp_path / "check.mjs"
    script.write_text("""
import assert from 'node:assert/strict';
const {parseArtifact} = await import(process.argv[2]);
const pointer = {v: 4, tab: 'tab-1', url: 'https://example.com',
    control: {wsBase: '/proxy/channel/run/9100/'},
    requires: ['nativeGuests'], native: {partition: 'p', device: 'desktop'},
    buttons: [{id: 'capture', label: 'Capture'}, {id: 'new_tab', label: 'New tab'}]};
const parsed = parseArtifact(JSON.stringify(pointer));
assert.deepEqual(parsed.buttons, pointer.buttons);
""")
    result = subprocess.run(["node", str(script), bundle.as_uri()],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
