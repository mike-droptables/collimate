// @vitest-environment jsdom
import {webcrypto} from 'node:crypto';
import {beforeEach,describe,expect,it,vi} from 'vitest';
import type {RendererSnapshotCodec} from './fileSnapshotCodec';
const records=vi.hoisted(()=>new Map<string,RendererSnapshotCodec&{approved?:boolean}>());
vi.mock('./fileDocumentStorage',()=>({codecJournal:{load:async(hash:string)=>records.get(hash),save:async(codec:RendererSnapshotCodec)=>records.set(codec.implementationHash,codec)}}));
vi.mock('./api/client',()=>({getApiUrl:()=>'/api'}));
let source:RendererSnapshotCodec;
const workers=vi.fn();
class TestWorker {
 onmessage:((event:{data:unknown})=>void)|null=null;onerror:((event:{message:string})=>void)|null=null;
 constructor(_url:string){workers();}
 postMessage(message:{requestId:string;payload:{text:string}}){queueMicrotask(()=>this.onmessage?.({data:{type:'materialized',requestId:message.requestId,body:message.payload.text}}));}
 terminate(){}
}
beforeEach(async()=>{
 vi.resetModules();records.clear();workers.mockClear();vi.stubGlobal('crypto',webcrypto);vi.stubGlobal('Worker',TestWorker);
 vi.stubGlobal('fetch',vi.fn());vi.stubGlobal('URL',Object.assign(URL,{createObjectURL:()=> 'blob:trusted',revokeObjectURL:()=>{}}));
 const text='self.onmessage = () => {};';const hash=Array.from(new Uint8Array(await webcrypto.subtle.digest('SHA-256',new TextEncoder().encode(text)))).map(n=>n.toString(16).padStart(2,'0')).join('');
 source={id:'milkdown-pm-json',version:1,implementationHash:hash,source:text};
});
describe('trusted snapshot decoder registry',()=>{
 it('shares one decoder when multiple documents recover concurrently',async()=>{
  records.set(source.implementationHash,{...source,approved:true});
  const registry=await import('./fileSnapshotCodec');
  expect(await Promise.all(['one','two'].map(text=>registry.materializeSnapshot({codecHash:source.implementationHash,snapshotId:text,payload:{text}})))).toEqual(['one','two']);expect(workers).toHaveBeenCalledTimes(1);
 });
 it('persists approved source before decoding and uses immutable exact request IDs',async()=>{
  vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({codecs:[source]}),{headers:{'Content-Type':'application/json'}}));
  const registry=await import('./fileSnapshotCodec');await registry.registerSnapshotCodec(source);
  expect(records.get(source.implementationHash)?.approved).toBe(true);
  expect(await registry.materializeSnapshot({codecHash:source.implementationHash,snapshotId:'transaction',payload:{text:'exact Markdown'}})).toBe('exact Markdown');expect(workers).toHaveBeenCalledTimes(1);
 });
 it('refuses caller-provided worker code absent from the trusted shipped manifest',async()=>{
  vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({codecs:[]})));
  const registry=await import('./fileSnapshotCodec');await expect(registry.registerSnapshotCodec(source)).rejects.toThrow('not approved');
  expect(records.size).toBe(0);expect(workers).not.toHaveBeenCalled();
 });
 it('rejects changed implementation bytes even when an approved hash is claimed',async()=>{
  const registry=await import('./fileSnapshotCodec');await expect(registry.registerSnapshotCodec({...source,source:'different'})).rejects.toThrow('does not match');expect(fetch).not.toHaveBeenCalled();
 });
 it('reload/offline recovery executes only a previously approved and revalidated decoder',async()=>{
  records.set(source.implementationHash,{...source,approved:true});vi.mocked(fetch).mockRejectedValue(new Error('offline'));
  const registry=await import('./fileSnapshotCodec');await registry.registerSnapshotCodec(source);
  expect(await registry.materializeSnapshot({codecHash:source.implementationHash,snapshotId:'recovered',payload:{text:'recovered exact body'}})).toBe('recovered exact body');expect(fetch).not.toHaveBeenCalled();
 });
});
