import { afterEach, beforeEach, expect, it, vi } from 'vitest';

vi.mock('./api', () => ({ getApiUrl: () => 'http://localhost:5113/api' }));
const hash = 'a'.repeat(64);
let jobs: {id: number}[];
let failWorker = false;
let stallWorker = false;
let workerCount: number;
class TestWorker {
  onmessage?: (event: {data: unknown}) => void;
  onerror?: () => void;
  onmessageerror?: () => void;
  constructor() { workerCount++; }
  terminate = vi.fn();
  postMessage(job: {id:number}) {
    jobs.push(job);
    if (stallWorker) return;
    queueMicrotask(() => this.onmessage?.({data: failWorker ? {id:job.id,error:'compiler syntax failure'}
      : {id:job.id,result:{source_hash:hash,code:'compiled',bundle_hash:'b'.repeat(64)}}}));
  }
}
beforeEach(() => {
  vi.resetModules(); jobs=[];failWorker=false;stallWorker=false;workerCount=0;
  vi.stubGlobal('window', {location:{href:'http://localhost:5113/',origin:'http://localhost:5113'}});
  vi.stubGlobal('Worker', TestWorker);
});
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
it('loads current prebuilt code without starting a compiler__F0251_F0295_F1868', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('export default {};')));
  const {loadRendererBundle} = await import('./rendererBundle');
  expect(await loadRendererBundle('prebuilt', hash, 'http://localhost:5113')).toBe('export default {};');
  expect(workerCount).toBe(0);
});
it('deduplicates misses and mounts the admitted winner after conflict__F0251_F0295_F1868', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(new Response('',{status:404}))
    .mockResolvedValueOnce(Response.json({source_hash:hash,entrypoint:'main.ts',files:{'main.ts':btoa('exact\r\n')}}))
    .mockResolvedValueOnce(new Response('',{status:409})).mockResolvedValueOnce(new Response('winning code'));
  vi.stubGlobal('fetch',fetch);
  const {loadRendererBundle} = await import('./rendererBundle');
  const first=loadRendererBundle('edited',hash,'http://localhost:5113');
  expect(loadRendererBundle('edited',hash,'http://localhost:5113')).toBe(first);
  expect(await first).toBe('winning code');
  expect(workerCount).toBe(1);expect(jobs).toHaveLength(1);
  expect(fetch.mock.calls[2][1].body).toBe(JSON.stringify({source_hash:hash,code:'compiled'}));
  expect(fetch.mock.calls[3][0]).toContain(`/source?v=${hash}`);
});
it('evicts rejected compilation so the next mount retries__F0251_F1868', async () => {
  const fetch=vi.fn().mockImplementation((url:string) => Promise.resolve(url.includes('/package')
    ? Response.json({source_hash:hash,entrypoint:'main.ts',files:{'main.ts':btoa('source')}})
    : url.includes('/bundle') ? new Response('ok') : new Response('',{status:404})));
  vi.stubGlobal('fetch',fetch);failWorker=true;
  const {loadRendererBundle}=await import('./rendererBundle');
  await expect(loadRendererBundle('retry',hash,'http://localhost:5113')).rejects.toThrow('compiler syntax failure');
  failWorker=false;fetch.mockImplementation((url:string) => Promise.resolve(url.includes('/package')
    ? Response.json({source_hash:hash,entrypoint:'main.ts',files:{'main.ts':btoa('source')}})
    : url.includes('/bundle') ? new Response('ok') : jobs.length > 1 ? new Response('fixed') : new Response('',{status:404})));
  expect(await loadRendererBundle('retry',hash,'http://localhost:5113')).toBe('fixed');
  expect(jobs).toHaveLength(2);
});
it('refuses stale package identity before compiling__F0251_F0295', async () => {
  vi.stubGlobal('fetch',vi.fn().mockResolvedValueOnce(new Response('',{status:404}))
    .mockResolvedValueOnce(Response.json({source_hash:'c'.repeat(64),entrypoint:'main.ts',files:{}})));
  const {loadRendererBundle}=await import('./rendererBundle');
  await expect(loadRendererBundle('stale',hash,'http://localhost:5113')).rejects.toThrow('no longer current');
  expect(workerCount).toBe(0);
});
it('routes bundle code only to the owning current frame request__F0251_P0F21', async () => {
  const target = {postMessage:vi.fn()} as unknown as Window;
  const other = {} as Window;
  const fetch=vi.fn(async () => new Response('module code'));
  vi.stubGlobal('fetch',fetch);
  const {answerRendererBundleRequest}=await import('./rendererBundle');
  const data={type:'RENDERER_BUNDLE_REQUEST',token:'d'.repeat(32),rendererId:'frame',rendererHash:hash};
  answerRendererBundleRequest({data,source:other,origin:window.location.origin} as MessageEvent,'frame',hash,target,()=>true);
  answerRendererBundleRequest({data,source:target,origin:'https://foreign.invalid'} as MessageEvent,'frame',hash,target,()=>true);
  answerRendererBundleRequest({data:{...data,rendererHash:'wrong'},source:target,origin:window.location.origin} as MessageEvent,'frame',hash,target,()=>true);
  expect(fetch).not.toHaveBeenCalled();
  answerRendererBundleRequest({data,source:target,origin:window.location.origin} as MessageEvent,'frame',hash,target,()=>false);
  await vi.waitFor(()=>expect(fetch).toHaveBeenCalledTimes(1));
  await new Promise(resolve=>setTimeout(resolve,0));
  expect(target.postMessage).not.toHaveBeenCalled();
  answerRendererBundleRequest({data,source:target,origin:window.location.origin} as MessageEvent,'frame',hash,target,()=>true);
  await vi.waitFor(()=>expect(target.postMessage).toHaveBeenCalledWith({type:'RENDERER_BUNDLE_RESULT',token:data.token,code:'module code'},window.location.origin));
});

it('terminates a stuck worker and permits a new attempt__F0251_F1868', async () => {
  vi.useFakeTimers();stallWorker=true;
  const {compileRendererPackage}=await import('./rendererBundle');
  const snapshot={source_hash:hash,entrypoint:'main.ts',files:{'main.ts':btoa('source')}};
  const pending=compileRendererPackage(snapshot);
  const rejected=expect(pending).rejects.toThrow('compilation timed out');
  await vi.advanceTimersByTimeAsync(30_000);await rejected;
  stallWorker=false;
  const retried=compileRendererPackage(snapshot);
  await vi.runAllTicks();
  expect((await retried).source_hash).toBe(hash);expect(workerCount).toBe(2);
});
it('a late stale admission never mounts the locally compiled code__F0251_F0295', async () => {
  vi.stubGlobal('fetch',vi.fn().mockResolvedValueOnce(new Response('',{status:404}))
    .mockResolvedValueOnce(Response.json({source_hash:hash,entrypoint:'main.ts',files:{'main.ts':btoa('source')}}))
    .mockResolvedValueOnce(new Response('',{status:409})).mockResolvedValueOnce(new Response('',{status:404})));
  const {loadRendererBundle}=await import('./rendererBundle');
  await expect(loadRendererBundle('late',hash,'http://localhost:5113')).rejects.toThrow('HTTP 404');
});

it('releases fulfilled source loads and checks the server again on remount__F0251_F1868', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(new Response('first code')).mockResolvedValueOnce(new Response('current code'));
  vi.stubGlobal('fetch', fetch);
  const {loadRendererBundle} = await import('./rendererBundle');
  expect(await loadRendererBundle('released', hash, 'http://localhost:5113')).toBe('first code');
  expect(await loadRendererBundle('released', hash, 'http://localhost:5113')).toBe('current code');
  expect(fetch).toHaveBeenCalledTimes(2);
});
