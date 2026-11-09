"""The shipped codec restores every Crepe feature without a DOM or network."""
import json
import os
from pathlib import Path
import subprocess

from tests._workspace import RENDERERS


ROOT = Path(__file__).resolve().parents[1]


def test_snapshot_worker_matches_live_crepe_and_rejects_invalid_structure():
    program = r'''
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { Worker } from 'node:worker_threads';
const descriptor = readFileSync(process.env.MILKDOWN_SNAPSHOT_CODEC,'utf8');
const codec = JSON.parse(descriptor.slice(descriptor.indexOf(' = ') + 3).trim().slice(0,-1));
const approved = JSON.parse(readFileSync('shared/renderer-runtime/snapshot-codecs.json','utf8'));
assert.deepEqual(approved.codecs,[{id:codec.id,version:codec.version,implementationHash:codec.implementationHash}]);
assert.equal(createHash('sha256').update(codec.source).digest('hex'),codec.implementationHash);
assert.doesNotMatch(codec.source, /(^|\n)\s*(import|export)\s/);
const worker = new Worker(`const {parentPort}=require('node:worker_threads');
globalThis.self=globalThis;self.postMessage=value=>parentPort.postMessage(value);
const events=new EventTarget();for(const key of ['addEventListener','removeEventListener','dispatchEvent'])globalThis[key]=events[key].bind(events);
${codec.source}
parentPort.on('message',data=>self.onmessage({data}));`, {eval:true,execArgv:[]});
let next=0;
const pending=new Map();
worker.on('message', result=>{
    const request=pending.get(result.requestId);
    assert.ok(request,'worker response retains its request identity');
    pending.delete(result.requestId);
    if(result.type==='error')request.reject(new Error(result.error));
    else request.resolve(result.body);
});
worker.on('error',error=>{for(const request of pending.values())request.reject(error);pending.clear();});
const materialize=payload=>new Promise((resolve,reject)=>{
    const requestId=String(++next);pending.set(requestId,{resolve,reject});
    worker.postMessage({type:'materialize',requestId,payload});
});
try {
    const fixtures=JSON.parse(readFileSync('tests/fixtures/milkdown-snapshots.json','utf8'));
    const nodes=new Set(),marks=new Set();
    const inspect=node=>{nodes.add(node.type);node.marks?.forEach(mark=>marks.add(mark.type));node.content?.forEach(inspect)};
    const actual=await Promise.all(fixtures.cases.map(async fixture=>{
        inspect(fixture.snapshot);
        const body=await materialize(JSON.stringify(fixture.snapshot));
        assert.equal(body,fixture.expected,fixture.markdown);
        return body;
    }));
    assert.equal(nodes.size,22);
    assert.equal(marks.size,5);
    // Crepe emits string image captions and boolean list spread, and
    // prosemirror-model validates every attribute in NodeType.create: a
    // snapshot carrying another type for one is rejected, never coerced.
    assert.ok(fixtures.cases.some(x=>JSON.stringify(x.snapshot).includes('"caption":""')));
    assert.ok(fixtures.cases.some(x=>JSON.stringify(x.snapshot).includes('"spread":false')));
    const image=fixtures.cases.find(x=>JSON.stringify(x.snapshot).includes('"caption":""'));
    await assert.rejects(materialize(JSON.stringify(image.snapshot).replace('"caption":""','"caption":null')), /caption/);
    const list=fixtures.cases.find(x=>JSON.stringify(x.snapshot).includes('"spread":false'));
    await assert.rejects(materialize(JSON.stringify(list.snapshot).replace('"spread":false','"spread":"false"')), /spread/);
    // An embed keeps its alt and writes its layout as a raw attribute block.
    assert.ok(fixtures.cases.some(x=>x.expected.includes('![plot](charts/plot.png){width=40% align=center}')));
    assert.ok(!fixtures.cases.some(x=>/!\[\d+\.\d\d\]\(/.test(x.expected)), 'no height ratio in an alt');
    for (const invalid of [null,{type:'unknown'},{type:'doc',content:{}},
        {type:'doc',content:[{type:'text',text:'invalid block child'}]},
        {type:'doc',content:[{type:'code_block',attrs:{language:{}},content:[{type:'text',text:'invalid language'}]}]},
        {type:'doc',content:[{type:'paragraph',content:[{type:'text',text:'x',marks:[{type:'unknown'}]}]}]}]) {
        await assert.rejects(materialize(JSON.stringify(invalid)));
    }
    await assert.rejects(materialize(fixtures.cases[1].snapshot), /encoded document snapshot/);
    await assert.rejects(materialize('{broken JSON'));
    assert.equal(await materialize(JSON.stringify(fixtures.cases[1].snapshot)),actual[1], 'a rejected snapshot does not poison the worker');
    console.log(JSON.stringify({cases:actual.length,nodeTypes:nodes.size,markTypes:marks.size}));
} finally {await worker.terminate();}
'''
    codec = RENDERERS / 'documents/milkdown/snapshot-codec.ts'
    result = subprocess.run(['node', '--input-type=module', '-e', program], cwd=ROOT,
                            env={**os.environ, 'MILKDOWN_SNAPSHOT_CODEC': str(codec)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'cases': 30, 'nodeTypes': 22, 'markTypes': 5}
