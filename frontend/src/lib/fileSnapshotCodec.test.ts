// @vitest-environment jsdom
import {webcrypto} from 'node:crypto';
import {beforeEach,describe,expect,it,vi} from 'vitest';
import type {JournaledCodec,RendererSnapshotCodec} from './fileSnapshotCodec';
const records=vi.hoisted(()=>new Map<string,JournaledCodec>());
vi.mock('./fileDocumentStorage',()=>({codecJournal:{load:async(hash:string)=>records.get(hash),save:async(codec:JournaledCodec)=>records.set(codec.implementationHash,codec)}}));
vi.mock('./api/client',()=>({getApiUrl:()=>'/api'}));
let source:RendererSnapshotCodec;
const workers=vi.fn();
class TestWorker {
 onmessage:((event:{data:unknown})=>void)|null=null;onerror:((event:{message:string})=>void)|null=null;
 constructor(_url:string){workers();}
 postMessage(message:{requestId:string;payload:{text:string}}){queueMicrotask(()=>this.onmessage?.({data:{type:'materialized',requestId:message.requestId,body:message.payload.text}}));}
 terminate(){}
}
const list=(...codecs:RendererSnapshotCodec[])=>new Response(JSON.stringify({codecs:codecs.map(({source:_source,...rest})=>rest)}));
// Each channel's trust list, answered per fetch from the URL's channel.
const lists=new Map<string,RendererSnapshotCodec[]>();
beforeEach(async()=>{
 vi.resetModules();records.clear();workers.mockClear();lists.clear();vi.stubGlobal('crypto',webcrypto);vi.stubGlobal('Worker',TestWorker);
 vi.stubGlobal('fetch',vi.fn(async(url:string)=>list(...(lists.get(decodeURIComponent(String(url).split('/')[3]))??[]))));
 vi.stubGlobal('URL',Object.assign(URL,{createObjectURL:()=> 'blob:trusted',revokeObjectURL:()=>{}}));
 const text='self.onmessage = () => {};';const hash=Array.from(new Uint8Array(await webcrypto.subtle.digest('SHA-256',new TextEncoder().encode(text)))).map(n=>n.toString(16).padStart(2,'0')).join('');
 source={id:'milkdown-pm-json',version:1,implementationHash:hash,source:text};
});
describe('per-channel snapshot codec trust',()=>{
 it('shares one decoder when multiple documents recover concurrently',async()=>{
  lists.set('a',[source]);records.set(source.implementationHash,{...source,channels:['a']});
  const registry=await import('./fileSnapshotCodec');
  expect(await Promise.all(['one','two'].map(text=>registry.materializeSnapshot('a',{codecHash:source.implementationHash,snapshotId:text,payload:{text}})))).toEqual(['one','two']);expect(workers).toHaveBeenCalledTimes(1);
 });
 it('asks the channel’s own list and journals the source with the channel before decoding',async()=>{
  lists.set('a',[source]);
  const registry=await import('./fileSnapshotCodec');await registry.registerSnapshotCodec('a',source);
  expect(vi.mocked(fetch).mock.calls.map(call=>String(call[0]))).toEqual(['/api/channels/a/snapshot-codecs']);
  expect(records.get(source.implementationHash)?.channels).toEqual(['a']);
  expect(await registry.materializeSnapshot('a',{codecHash:source.implementationHash,snapshotId:'transaction',payload:{text:'exact Markdown'}})).toBe('exact Markdown');expect(workers).toHaveBeenCalledTimes(1);
 });
 it('trusts a codec in the channel whose Capabilities hold its renderer and not in another',async()=>{
  lists.set('a',[source]);lists.set('b',[]);
  const registry=await import('./fileSnapshotCodec');await registry.registerSnapshotCodec('a',source);
  await expect(registry.registerSnapshotCodec('b',source)).rejects.toThrow('not trusted in this channel');
  await expect(registry.materializeSnapshot('b',{codecHash:source.implementationHash,snapshotId:'x',payload:{text:'x'}})).rejects.toThrow('unavailable');
  expect(records.get(source.implementationHash)?.channels).toEqual(['a']);expect(workers).not.toHaveBeenCalled();
 });
 it('refuses caller-provided worker code absent from the channel’s list',async()=>{
  const registry=await import('./fileSnapshotCodec');await expect(registry.registerSnapshotCodec('a',source)).rejects.toThrow('not trusted in this channel');
  expect(records.size).toBe(0);expect(workers).not.toHaveBeenCalled();
 });
 it('asks the channel again after a refusal, so a renderer added meanwhile registers',async()=>{
  const registry=await import('./fileSnapshotCodec');await expect(registry.registerSnapshotCodec('a',source)).rejects.toThrow('not trusted');
  lists.set('a',[source]);await registry.registerSnapshotCodec('a',source);
  expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2);expect(records.get(source.implementationHash)?.channels).toEqual(['a']);
 });
 it('forgets the channel in the journal once its list stops naming the codec',async()=>{
  records.set(source.implementationHash,{...source,channels:['a','b']});
  const registry=await import('./fileSnapshotCodec');await expect(registry.registerSnapshotCodec('a',source)).rejects.toThrow('not trusted');
  expect(records.get(source.implementationHash)?.channels).toEqual(['b']);
 });
 it('rejects changed implementation bytes even when a trusted hash is claimed',async()=>{
  const registry=await import('./fileSnapshotCodec');await expect(registry.registerSnapshotCodec('a',{...source,source:'different'})).rejects.toThrow('does not match');expect(fetch).not.toHaveBeenCalled();
 });
 it('while the list is unreachable, recovery runs only a decoder journaled for that channel',async()=>{
  records.set(source.implementationHash,{...source,channels:['a']});vi.mocked(fetch).mockRejectedValue(new Error('offline'));
  const registry=await import('./fileSnapshotCodec');await registry.registerSnapshotCodec('a',source);
  expect(await registry.materializeSnapshot('a',{codecHash:source.implementationHash,snapshotId:'recovered',payload:{text:'recovered exact body'}})).toBe('recovered exact body');
  await expect(registry.registerSnapshotCodec('b',source)).rejects.toThrow('unreachable');
  await expect(registry.materializeSnapshot('b',{codecHash:source.implementationHash,snapshotId:'other',payload:{text:'x'}})).rejects.toThrow('unavailable');
 });
});
