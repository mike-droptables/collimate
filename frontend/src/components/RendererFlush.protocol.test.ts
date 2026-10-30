import {afterEach,expect,it,vi} from 'vitest';
import {flushSandbox,withFlushedEditors,registerRendererFlusher,flushRendererEditors} from '@/lib/rendererFlush';
const local=vi.hoisted(()=>({durable:vi.fn(async()=>{})}));
vi.mock('@/lib/fileDocuments',()=>({ensureLocalFileDocuments:local.durable}));

afterEach(()=>{vi.useRealTimers();local.durable.mockReset();local.durable.mockResolvedValue(undefined);});

it('accepts only the current window, origin and request acknowledgment and forgets settled work __F0684_F0691_F0699',async()=>{
  const target={postMessage:vi.fn()} as unknown as Window;
  let current=true;
  const first=flushSandbox(target,()=>current);
  expect(flushSandbox(target,()=>current)).toBe(first);
  const request=vi.mocked(target.postMessage).mock.calls[0][0];
  let settled=false;void first.then(()=>{settled=true;});
  const answer=(source:Window,origin:string,requestId:string,error?:string)=>window.dispatchEvent(new MessageEvent('message',{source,origin,data:{type:'FLUSH_DRAFT_ACK',requestId,error}}));
  answer(window,window.location.origin,request.requestId);
  answer(target,'https://foreign.example',request.requestId);
  answer(target,window.location.origin,'another-request');
  await Promise.resolve();expect(settled).toBe(false);
  answer(target,window.location.origin,request.requestId);await first;
  const second=flushSandbox(target,()=>current);
  expect(second).not.toBe(first);
  const refused=expect(second).rejects.toThrow('serializer failed');
  const next=vi.mocked(target.postMessage).mock.calls[1][0];
  answer(target,window.location.origin,next.requestId,'serializer failed');await refused;
  const stale=flushSandbox(target,()=>current);current=false;
  const staleFailure=expect(stale).rejects.toThrow('changed');
  answer(target,window.location.origin,vi.mocked(target.postMessage).mock.calls[2][0].requestId);
  await staleFailure;
});

it('bounds a missing iframe acknowledgment and permits an explicit retry __F0691_F0694_P4F10',async()=>{
  vi.useFakeTimers();
  const target={postMessage:vi.fn()} as unknown as Window;
  const missing=flushSandbox(target);
  const refused=expect(missing).rejects.toThrow('Still saving, try again in a moment.');
  await vi.advanceTimersByTimeAsync(5000);await refused;
  const retry=flushSandbox(target);
  const request=vi.mocked(target.postMessage).mock.calls[1][0];
  window.dispatchEvent(new MessageEvent('message',{source:target,origin:window.location.origin,data:{type:'FLUSH_DRAFT_ACK',requestId:request.requestId}}));
  await retry;
});


it('channel replacement waits for serialization and local durability, and refusal never replaces it __F0691_F0694_F2003',async()=>{
  let release!:()=>void,releaseLocal!:()=>void;const order:string[]=[];
  const held=new Promise<void>(resolve=>{release=resolve;});
  const localHeld=new Promise<void>(resolve=>{releaseLocal=resolve;});
  local.durable.mockImplementation(async()=>{order.push('local');await localHeld;});
  const offEditor=registerRendererFlusher('channel-cell','notes.txt',async()=>{order.push('editor');await held;});
  try {
    const replace=vi.fn(async()=>{order.push('replace');});
    const change=withFlushedEditors(replace);
    await Promise.resolve();await Promise.resolve();
    expect(replace).not.toHaveBeenCalled();release();
    await vi.waitFor(()=>expect(local.durable).toHaveBeenCalledOnce());
    expect(replace).not.toHaveBeenCalled();releaseLocal();await change;
    expect(order).toEqual(['editor','local','replace']);
    offEditor();
    const offRefused=registerRendererFlusher('channel-cell','notes.txt',async()=>{throw new Error('refused pending editor');});
    try {await expect(withFlushedEditors(replace)).rejects.toThrow('refused pending editor');expect(replace).toHaveBeenCalledTimes(1);}
    finally{offRefused();}
    local.durable.mockRejectedValueOnce(new Error('local journal unavailable'));
    await expect(withFlushedEditors(replace)).rejects.toThrow('local journal unavailable');
    expect(replace).toHaveBeenCalledTimes(1);
  }finally{release?.();releaseLocal?.();offEditor();}
});


it('forgets a timed-out native callback so an explicit repaired retry can serialize __F0691_F1808_P4F10',async()=>{
  vi.useFakeTimers();let repaired=false;
  const serialize=vi.fn(()=>repaired ? undefined : new Promise<void>(()=>{}));
  const off=registerRendererFlusher('repair-cell','pending.txt',serialize);
  try {
    const first=flushRendererEditors('repair-cell');
    const refused=expect(first).rejects.toThrow('Still saving, try again in a moment.');
    await vi.advanceTimersByTimeAsync(5000);await refused;
    repaired=true;await flushRendererEditors('repair-cell');
    expect(serialize).toHaveBeenCalledTimes(2);
  }finally{off();}
});
