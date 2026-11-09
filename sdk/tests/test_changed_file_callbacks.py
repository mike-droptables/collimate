"""Execute the renderers' actual subscription callbacks with counted reads."""
import json
import subprocess
import pytest

from tests._workspace import BUILTINS


def _callback(source: str, marker: str) -> str:
    start = source.index("=> {", source.index(marker)) + len("=> {")
    depth = 1
    for end in range(start, len(source)):
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
            if depth == 0:
                return source[start:end]
    raise AssertionError("unterminated callback")


@pytest.mark.parametrize("file,marker,path", [
    ("live/assistant/chat-runtime.ts", "const off = api.onFilesChange", "chat/thread.json"),
    ("live/assistant/main.tsx", "const offFiles = api.onFilesChange", "pointer.chat"),
])
def test_unrelated_commits_do_not_read_renderer_files__F1716(file, marker, path):
    body = _callback((BUILTINS / file).read_text(), marker)
    script = r'''
const assert = require('node:assert/strict');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const callback = new AsyncFunction('env', 'detail', 'with(env) {' + BODY + '}');
let reads = 0;
const env = {
    pointer: {document: 'document.html'}, sidecarPath: 'document.html.comments.yaml',
    activeRef: {current: 'thread'}, threadFile: () => 'chat/thread.json',
    api: {getFilename: () => 'pointer.chat', getSiblingFile: async () => { reads++; return 'fresh'; }},
    load: () => { reads++; }, loadThread: () => { reads++; }, refreshPointer: () => { reads++; },
    setTimeout: (fn) => { fn(); return 1; }, clearTimeout: () => {}, timer: null,
    fetchSeq: 0, localSeq: 0, cancelled: false, replaceContent: async () => {},
    reloadTimer: undefined,
    window: {setTimeout: (fn) => { fn(); return 1; }, clearTimeout: () => {}},
};
(async () => {
    await callback(env, {changedFiles: ['unrelated.md']});
    await callback(env, {changedFiles: []});
    assert.equal(reads, 0, 'unrelated and empty commits must perform no read');
    await callback(env, {changedFiles: [PATH]});
    assert.equal(reads, 1, 'the subscribed file must refresh');
})().catch(e => { console.error(e); process.exitCode = 1; });
'''.replace("BODY", json.dumps(body)).replace("[PATH]", json.dumps([path]))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
