import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {ANNOTATION_ACK_MS,AnnotationUnavailableError,requestSandboxAnnotation,type AnnotationRequest} from '@/lib/rendererAnnotation';

// The host's half of the sandbox annotation wire (sandbox.html answers the
// other): one request to one frame, answered only from that frame's window,
// this origin and this request id, never re-sent, and cancelled in the frame
// however the host gives up on it.

const REQUEST:AnnotationRequest={id:'req-1',mode:'pick',hint:'Click an element'};
const CONTEXT={label:'page.html › #sec',text:'Element',anchor:{css:'#sec'}};
const frame=()=>({postMessage:vi.fn()}) as unknown as Window;
const posted=(target:Window)=>vi.mocked(target.postMessage).mock.calls.map(call=>call as unknown[]);
const say=(source:unknown,data:Record<string,unknown>,origin=window.location.origin)=>
  window.dispatchEvent(new MessageEvent('message',{source:source as Window,origin,data}));
const ack=(target:Window,requestId=REQUEST.id)=>say(target,{type:'ANNOTATION_ACK',requestId});
const result=(target:Window,fields:Record<string,unknown>,requestId=REQUEST.id)=>say(target,{type:'ANNOTATION_RESULT',requestId,...fields});
const unavailable=(promise:Promise<unknown>)=>promise.then(()=>{throw new Error('resolved');},(error:unknown)=>error as AnnotationUnavailableError);

beforeEach(()=>{vi.useFakeTimers();});
afterEach(()=>{vi.useRealTimers();});

it('posts the request to the frame at this origin, and only that frame\'s answer to it counts',async()=>{
  const target=frame();
  const pending=requestSandboxAnnotation(target,REQUEST,new AbortController().signal,()=>true);
  expect(posted(target)).toEqual([[{type:'ANNOTATION_REQUEST',requestId:'req-1',mode:'pick',hint:'Click an element'},window.location.origin]]);
  let settled=false;void pending.then(()=>{settled=true;});
  result(window,{context:CONTEXT});
  result(target,{context:CONTEXT},'another-request');
  say(target,{type:'ANNOTATION_RESULT',requestId:REQUEST.id,context:CONTEXT},'https://foreign.example');
  say(target,{type:'SOMETHING_ELSE',requestId:REQUEST.id});
  say(target,'text' as unknown as Record<string,unknown>);
  await vi.advanceTimersByTimeAsync(0);expect(settled).toBe(false);
  result(target,{context:CONTEXT});
  await expect(pending).resolves.toEqual(CONTEXT);
});

it('sends no hint the request lacks',()=>{
  const target=frame();
  void requestSandboxAnnotation(target,{id:'req-2',mode:'selection'},new AbortController().signal,()=>true).catch(()=>{});
  expect(posted(target)[0][0]).toEqual({type:'ANNOTATION_REQUEST',requestId:'req-2',mode:'selection'});
});

it('a frame that never acknowledges is not capable, after the 2 s the renderer guide promises',async()=>{
  const target=frame();
  const refused=unavailable(requestSandboxAnnotation(target,REQUEST,new AbortController().signal,()=>true));
  let settled=false;void refused.then(()=>{settled=true;});
  // sdk/shared/docs/how-to-write-a-renderer.md: "the host waits 2 s for that".
  await vi.advanceTimersByTimeAsync(1999);
  expect(settled).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  expect(ANNOTATION_ACK_MS).toBe(2000);
  const error=await refused;
  expect(error).toBeInstanceOf(AnnotationUnavailableError);
  expect(error.reason).toBe('not-capable');
  expect(error.message).toBe('The renderer did not answer.');
  // A frame that acknowledges late does not keep a pick nobody waits for.
  expect(posted(target)[1]).toEqual([{type:'ANNOTATION_CANCEL',requestId:'req-1'},window.location.origin]);
});

it('after the acknowledgment the user takes as long as they like',async()=>{
  const target=frame();
  const pending=requestSandboxAnnotation(target,REQUEST,new AbortController().signal,()=>true);
  ack(target);
  await vi.advanceTimersByTimeAsync(60_000);
  result(target,{context:CONTEXT});
  await expect(pending).resolves.toEqual(CONTEXT);
});

describe('the result',()=>{
  const answer=async(fields:Record<string,unknown>)=>{
    const target=frame();
    const pending=requestSandboxAnnotation(target,REQUEST,new AbortController().signal,()=>true);
    ack(target);result(target,fields);
    return pending;
  };
  it('maps each answer',async()=>{
    const unsupported=await unavailable(answer({unsupported:true}));
    expect([unsupported.reason,unsupported.message]).toEqual(['not-capable','The renderer does not take annotations.']);
    const refused=await unavailable(answer({unavailable:'This page answers element picks only.'}));
    expect([refused.reason,refused.message]).toEqual(['renderer','This page answers element picks only.']);
    await expect(answer({error:'boom'})).rejects.toThrow('boom');
    await expect(answer({cancelled:true})).resolves.toBeNull();
    await expect(answer({context:null})).resolves.toBeNull();
    await expect(answer({})).resolves.toBeNull();
    await expect(answer({context:CONTEXT})).resolves.toEqual(CONTEXT);
  });
});

it('an abort tells the current frame, rejects with its reason and ignores a late answer',async()=>{
  const target=frame();const controller=new AbortController();const reason={kind:'cancel'};
  const pending=requestSandboxAnnotation(target,REQUEST,controller.signal,()=>true);
  ack(target);
  controller.abort(reason);
  await expect(pending).rejects.toBe(reason);
  expect(posted(target)[1]).toEqual([{type:'ANNOTATION_CANCEL',requestId:'req-1'},window.location.origin]);
  result(target,{context:CONTEXT});
  expect(posted(target)).toHaveLength(2);
});

it('a frame the host let go rejects parked, and its pick is cancelled once',async()=>{
  const target=frame();let current=true;const controller=new AbortController();
  const refused=unavailable(requestSandboxAnnotation(target,REQUEST,controller.signal,()=>current));
  ack(target);current=false;
  await vi.advanceTimersByTimeAsync(250);
  const error=await refused;
  expect([error.reason,error.message]).toEqual(['parked','The file’s view was replaced.']);
  controller.abort();
  expect(posted(target).slice(1)).toEqual([[{type:'ANNOTATION_CANCEL',requestId:'req-1'},window.location.origin]]);
});

it('an abort after the host let go (its view unregistering) still cancels the frame\'s pick',async()=>{
  const target=frame();const controller=new AbortController();
  const pending=requestSandboxAnnotation(target,REQUEST,controller.signal,()=>false);
  ack(target);
  controller.abort('gone');
  await expect(pending).rejects.toBe('gone');
  expect(posted(target).slice(1)).toEqual([[{type:'ANNOTATION_CANCEL',requestId:'req-1'},window.location.origin]]);
});

it('an already aborted signal sends nothing',async()=>{
  const target=frame();const controller=new AbortController();controller.abort('early');
  await expect(requestSandboxAnnotation(target,REQUEST,controller.signal,()=>true)).rejects.toBe('early');
  expect(posted(target)).toEqual([]);
});
