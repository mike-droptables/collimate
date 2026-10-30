import {afterEach,beforeEach,expect,it,vi} from 'vitest';
vi.mock('./previewDependencies',()=>({edges:async(body:string)=>({imports:[],body}),freezeGraph:async()=>({}),graphImports:async()=>({}),hashText:()=> 'artifact',readPrepared:async()=>({}),writePrepared:async()=>{}}));
const compile=vi.hoisted(()=>vi.fn());
vi.mock('./rendererBundle',()=>({compileRendererFiles:compile}));
vi.mock('./api',()=>({getApiUrl:()=>'/api'}));
beforeEach(()=>{vi.stubGlobal('window',{location:{href:'http://localhost:5113/',origin:'http://localhost:5113'}});vi.resetModules();compile.mockReset().mockResolvedValue({source_hash:'a',code:'code',bundle_hash:'b'});});
afterEach(()=>vi.unstubAllGlobals());
it('retries failed served runtime fetches and never admits preview output __D14',async()=>{
 const fetch=vi.fn().mockResolvedValueOnce(new Response('',{status:503})).mockResolvedValueOnce(new Response('session')).mockResolvedValueOnce(new Response('{}')).mockImplementation(async(url)=>new Response(String(url).endsWith('.json')?'{}':'served runtime'));
 vi.stubGlobal('fetch',fetch);
 const {compilePreview}=await import('./rendererPreview');
 await expect(compilePreview({'main.ts':new Uint8Array([1])},'main.ts')).rejects.toThrow('HTTP 503');
 const result=await compilePreview({'main.ts':new Uint8Array([2])},'main.ts');
 expect(result).toMatchObject({code:'code',runtime:{react:'served runtime',session:'served runtime'},apiOrigin:window.location.origin});
 expect(fetch.mock.calls.every(([url])=>String(url).includes('/api/sdk/'))).toBe(true);
 expect(compile.mock.calls[1][0]['main.ts']).toEqual(new Uint8Array([2]));
});
it('bounds files, bytes, concurrent compiles and runtime bytes __D14',async()=>{
 vi.stubGlobal('fetch',vi.fn(async(url)=>new Response(String(url).endsWith('.json')?'{}':'runtime')));
 const {compilePreview}=await import('./rendererPreview');
 await expect(compilePreview(Object.fromEntries(Array.from({length:5001},(_,i)=>[`${i}`,new Uint8Array()])), '0')).rejects.toThrow('5000');
 await expect(compilePreview({'x':new Uint8Array(64*1024*1024+1)},'x')).rejects.toThrow('64 MiB');
 let resolve!: (value:unknown)=>void;compile.mockImplementation(()=>new Promise(r=>{resolve=r;}));
 // Distinct deferred jobs retain all four active ownership slots.
 const resolves:Array<(value:unknown)=>void>=[];compile.mockImplementation(()=>new Promise(r=>{resolve=r;resolves.push(resolve);}));
 const jobs=Array.from({length:4},()=>compilePreview({'x':new Uint8Array()},'x'));
 await expect(compilePreview({'x':new Uint8Array()},'x')).rejects.toThrow('busy');
 for(const done of resolves)done({code:'x'});await Promise.all(jobs);
 vi.resetModules();vi.stubGlobal('fetch',vi.fn(async()=>new Response(new Uint8Array(2*1024*1024+1))));
 compile.mockResolvedValue({code:'x'});
 const fresh=await import('./rendererPreview');await expect(fresh.compilePreview({'x':new Uint8Array()},'x')).rejects.toThrow('two MiB');
});
it('requests membership with the immutable version and refuses malformed host results __D14',async()=>{
 const fetch=vi.fn().mockResolvedValueOnce(Response.json({paths:['a','dir/b']})).mockResolvedValueOnce(Response.json({paths:[1]}));vi.stubGlobal('fetch',fetch);
 const {rendererFilePaths}=await import('./rendererPreview');expect(await rendererFilePaths('channel name','cell','version')).toEqual(['a','dir/b']);
 expect(fetch.mock.calls[0][0]).toContain('/channel%20name/cells/cell/file-paths?version_id=version');
 await expect(rendererFilePaths('channel name','cell')).rejects.toThrow('Invalid renderer file paths');
});
