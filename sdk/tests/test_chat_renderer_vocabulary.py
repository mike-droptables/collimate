# SPDX-License-Identifier: MIT
"""Chat session identity stays in its owner, and the test bundler reads YAML."""
from pathlib import Path
import subprocess

from tests._workspace import BUILTINS


REPO = Path(__file__).resolve().parents[1]


def _check_module(tmp_path, entrypoint, source):
    bundle = tmp_path / "renderer.mjs"
    build = subprocess.run([
        "node", str(REPO / "scripts/bundle_renderer_test.mjs"), str(entrypoint), str(bundle),
    ], capture_output=True, text=True, cwd=REPO)
    assert build.returncode == 0, build.stderr
    script = tmp_path / "check.mjs"
    script.write_text(source)
    result = subprocess.run(["node", str(script), bundle.as_uri()],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_chat_surface_pointer_parser_uses_chat_id__F0287(tmp_path):
    _check_module(tmp_path, BUILTINS / "live/assistant/message-model.ts", """
import assert from 'node:assert/strict';
const {parsePointer} = await import(process.argv[2]);
const input = {chat_id: 'chat-test', ws_path: '/api/chats/chat-test', token: 't',
               label: 'Test', threads_dir: 'threads', active_thread: 'main', threads: []};
const expected = {...input, starting: false};
assert.deepEqual(parsePointer(JSON.stringify(input)), expected);
""")


def test_test_bundler_yaml_namespace_and_named_exports_use_real_parser__D21_D22_F1817(tmp_path):
    entry = tmp_path / "yaml-entry.ts"
    entry.write_text("""
import * as yaml from 'https://esm.sh/js-yaml@5.5.0?target=es2022';
import {load, dump} from 'https://esm.sh/js-yaml@5.5.0?target=es2022';
export {yaml, load, dump};
""")
    _check_module(tmp_path, entry, r"""
import assert from 'node:assert/strict';
const {yaml, load, dump} = await import(process.argv[2]);
assert.equal(yaml.load, load);
assert.equal(yaml.dump, dump);
const input = 'document: "report: draft.md"\nnotes: report.notes\noptions:\n  enabled: true\n  labels: [one, two]\n';
const expected = {document: 'report: draft.md', notes: 'report.notes',
                  options: {enabled: true, labels: ['one', 'two']}};
assert.deepEqual(yaml.load(input), expected);
assert.deepEqual(load(dump(expected)), expected);
assert.throws(() => yaml.load('broken: [unterminated'));
assert.equal('default' in yaml, false);
""")
