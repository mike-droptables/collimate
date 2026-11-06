"""Cross-language exact package framing, including UTF8 order and binary bytes."""

import base64
import json
import subprocess

from collimate._host.package_hash import content_hash

from tests._workspace import BUILTINS


def test_preview_and_approval_hash_exact_bytes_agree__D14_D16():
    files = {"z.txt": b"a\r\n", "é.txt": b"\x00\xff", "a/b.ts": b"export default 1", "😀": b"x"}
    module = (
        BUILTINS / "definitions/renderer-example/package_hash.js"
    )
    script = f"""import {{contentHash}} from {json.dumps(module.as_uri())};
import {{createHash}} from 'node:crypto';
const files=Object.fromEntries(Object.entries(JSON.parse(process.argv[1])).map(([p,b])=>[p,Buffer.from(b,'base64')]));
console.log(contentHash(files,b=>new Uint8Array(createHash('sha256').update(b).digest())));"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            json.dumps({p: base64.b64encode(b).decode() for p, b in files.items()}),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout.strip() == content_hash(files)
    assert content_hash(files) != content_hash(files | {"z.txt": b"a\n"})


def test_preview_policy_guards_paths_hard_lock_and_owned_messages__D14():
    module = (
        BUILTINS / "definitions/renderer-example/policy.js"
    )
    script = f"""import {{writeAllowed,previewPolicy,validMessage}} from {json.dumps(module.as_uri())};
import assert from 'node:assert/strict';
for(const path of ['../outside','/absolute','a//b','a/./b','a/../b','C:/file','a\\\\b','pkg/main.tsx','pkg/sub/a','x.renderer','x.generator','x.runner','x.handler','notes.options_stack','notes.OPTIONS_STACK','x.HANDLER','x.Renderer','x.GENERATOR','x.RUNNER'])assert.equal(writeAllowed(path,'pkg',true,false),false,path);
assert.equal(writeAllowed('notes/a.txt','pkg',true,false),true);
assert.equal(writeAllowed('notes/a.txt','pkg',false,false),false);
assert.equal(writeAllowed('notes/a.txt','pkg',true,true),false);
const source={{}},frame={{contentWindow:source}},data={{tag:'collimate-preview',token:'token',id:1}};
assert.equal(validMessage({{source,origin:'null',data}},frame,'token'),true);
assert.equal(validMessage({{source:{{}},origin:'null',data}},frame,'token'),false);
assert.equal(validMessage({{source,origin:'https://foreign.invalid',data}},frame,'token'),false);
assert.equal(validMessage({{source,origin:'null',data}},frame,'stale'),false);
assert.match(previewPolicy('https://api.example:8443','https://app.example/fonts/fonts.css'),/connect-src 'none'/);
assert.throws(()=>previewPolicy('data:text/plain,x','https://app.example/fonts/fonts.css'));
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
