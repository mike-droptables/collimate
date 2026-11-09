import {afterEach,expect,it,vi} from 'vitest';
const state=vi.hoisted(()=>({graph:{} as Record<string,{body:string;hash:string}>}));
vi.mock('./api',()=>({getApiUrl:()=>'/api'}));
vi.mock('./rendererBundle',()=>({compileRendererFiles:async()=>({source_hash:'unchanged-package',code:'import {value} from "https://esm.sh/example@1.2.3";export default value;',bundle_hash:'unchanged-bundle'})}));
vi.mock('./previewDependencies',async original=>({...await original<typeof import('./previewDependencies')>(),readPrepared:async()=>state.graph,writePrepared:async(_key:string,graph:typeof state.graph)=>{state.graph=graph;}}));
afterEach(()=>vi.unstubAllGlobals());
it('P4F04_P5F05 actual compilation artifact changes with frozen bodies while package identity and approval stay unchanged',async()=>{
 vi.stubGlobal('window',{location:{href:'http://localhost:5113/'}});
 const fetch=vi.fn(async(url:string)=>new Response(url.endsWith('.json')?'{}':'export const runtime=1;'));vi.stubGlobal('fetch',fetch);
 const {hashText}=await import('./previewDependencies');const {compilePreview}=await import('./rendererPreview');
 state.graph={'https://esm.sh/example@1.2.3':{body:'export const value=1;',hash:hashText('export const value=1;')}};
 const first=await compilePreview({'main.js':new Uint8Array()},'main.js');
 state.graph={'https://esm.sh/example@1.2.3':{body:'export const value=2;',hash:hashText('export const value=2;')}};
 const second=await compilePreview({'main.js':new Uint8Array()},'main.js');
 expect(first.artifact_hash).not.toBe(second.artifact_hash);expect(first.source_hash).toBe(second.source_hash);expect(first.bundle_hash).toBe(second.bundle_hash);expect(first.imports).not.toEqual(second.imports);
 expect(fetch.mock.calls.every(([url])=>url.startsWith('http://localhost:5113/api/sdk/'))).toBe(true);
 state.graph['https://esm.sh/example@1.2.3'].body='changed without hash';
 await expect(compilePreview({'main.js':new Uint8Array()},'main.js')).rejects.toThrow('hash differs');
});
