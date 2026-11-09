# SPDX-License-Identifier: MIT
"""Guest lifetime keys retain their run and tab identity."""
from pathlib import Path
import subprocess

from tests._workspace import BUILTINS


def test_guest_session_key_helper_preserves_run_tab_identity__F0287(tmp_path):
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
const {parseArtifact, guestSessionKeyOf} = await import(process.argv[2]);
assert.equal(typeof guestSessionKeyOf, 'function');
const make = (base, tab) => parseArtifact(JSON.stringify({v: 4, tab,
    control: {wsBase: base}, url: 'https://example.com',
    requires: ['nativeGuests'], native: {partition: 'p', device: 'desktop'}}));
assert.equal(guestSessionKeyOf(make('/proxy/channel/run/9100/', 'tab-1')),
             '/proxy/channel/run/9100/#tab-1');
assert.equal(guestSessionKeyOf(make('/proxy/channel/run/9100/', 'tab-1')),
             guestSessionKeyOf(make('/proxy/channel/run/9100/', 'tab-1')));
assert.notEqual(guestSessionKeyOf(make('/proxy/channel/run/9100/', 'tab-1')),
                guestSessionKeyOf(make('/proxy/channel/run/9100/', 'tab-2')));
assert.notEqual(guestSessionKeyOf(make('/proxy/channel/run/9100/', 'tab-1')),
                guestSessionKeyOf(make('/proxy/channel/new-run/9100/', 'tab-1')));
""")
    result = subprocess.run(["node", str(script), bundle.as_uri()],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
