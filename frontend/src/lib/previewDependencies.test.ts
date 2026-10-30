import {afterEach,expect,it,vi} from 'vitest';
import {edges,freezeGraph,graphImports,hashText,verifyGraph,pinnedURL} from './previewDependencies';
const root='https://esm.sh/example@1.2.3?target=es2022';
const child='https://esm.sh/example@1.2.3/es2022/example.mjs';
const module=(body:string)=>({body,hash:hashText(body)});
// The app's API base; module-CDN bodies are fetched from its mirror.
const API='http://app.test/api';
const mirrored=(url:string)=>url.replace('https://',`${API}/mirror/`);
afterEach(()=>vi.unstubAllGlobals());
it('P4F04_P5F05 preparation freezes literal export and cyclic import edges without executing hostile module bodies',async()=>{
 const marker=vi.fn();vi.stubGlobal('previewExecuted',marker);
 // The mirror's bodies: same-host specifiers are relative, so they resolve against the upstream key.
 const bodies={[mirrored(root)]:`export * from './example@1.2.3/es2022/example.mjs';globalThis.previewExecuted();`,[mirrored(child)]:`import '../../example@1.2.3?target=es2022';export const value=7;`};
 const fetch=vi.fn(async(url:string)=>new Response(bodies[url as keyof typeof bodies]));vi.stubGlobal('fetch',fetch);
 const graph=await freezeGraph([root],{},{},true,API);expect(fetch.mock.calls.map(call=>call[0])).toEqual([mirrored(root),mirrored(child)]);expect(marker).not.toHaveBeenCalled();expect(graph[root]).toEqual(module(bodies[mirrored(root)]));
 expect(Object.keys(graph)).toEqual([root,child]);
 const imports=await graphImports(graph);expect(Object.keys(imports)).toEqual([root,child]);expect(atob(imports[root].split(',')[1])).toContain(child);expect(atob(imports[child].split(',')[1])).toContain(root);
});
it('preparation fetches only module-CDN URLs from the mirror; another origin is fetched as named',async()=>{
 const other='https://unpkg.com/demo@1.2.3/module.js';const jsdelivr='https://cdn.jsdelivr.net/npm/demo@1.2.3/dist/main.js';
 const bodies={[other]:`export * from '${jsdelivr}';`,[mirrored(jsdelivr)]:'export const value=1;'};
 const fetch=vi.fn(async(url:string,_init?:RequestInit)=>new Response(bodies[url as keyof typeof bodies]));vi.stubGlobal('fetch',fetch);
 const graph=await freezeGraph([other],{},{},true,API);
 expect(fetch.mock.calls.map(call=>call[0])).toEqual([other,mirrored(jsdelivr)]);
 expect(fetch.mock.calls.every(([,init])=>init?.credentials==='omit'&&init?.redirect==='error')).toBe(true);
 expect(Object.keys(graph)).toEqual([other,jsdelivr]);
});
it('P4F04_P5F05 unprepared preview clearly refuses without network and common prepared modules succeed offline',async()=>{
 const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
 await expect(freezeGraph([root],{},{},false,API)).rejects.toThrow('Choose Prepare dependencies');
 const common={[root]:module('export const value=1;')};expect(await freezeGraph([root],common,{},false,API)).toEqual(common);expect(fetch).not.toHaveBeenCalled();
});
it('P4F04_P5F05 computed imports and nonpinned graph edges fail closed',async()=>{
 expect((await edges('const name="x";import(name)')).body).toContain('__collimatePreviewImport(name)');
 await expect(edges('export * from "https://esm.sh/example@latest"')).rejects.toThrow('pinned');
 await expect(freezeGraph([root],{[root]:module('export * from "https://other.invalid/a.js"')},{},false,API)).rejects.toThrow('pinned');
});
it('P4F04_P5F05 local snapshot hash corruption refuses and module bytes change artifact identity',async()=>{
 await expect(verifyGraph({[root]:{body:'changed',hash:hashText('original')}})).rejects.toThrow('hash differs');
 expect(hashText(JSON.stringify({[root]:module('a')}))).not.toBe(hashText(JSON.stringify({[root]:module('b')})));
});

it('P4F04_P5F05 pinned roots preserve CDN choice and immutable hashes, while child polyfills freeze exact bytes',async()=>{
 expect(pinnedURL('https://cdn.jsdelivr.net/npm/demo@1.2.3/dist/main.js')).toContain('jsdelivr');
 expect(pinnedURL('https://unpkg.com/demo@1.2.3/module.js')).toContain('unpkg');
 expect(pinnedURL('https://modules.example/0123456789abcdef0123456789abcdef01234567/main.js')).toContain('modules.example');
 for(const url of ['https://unpkg.com/demo@latest/module.js','https://unpkg.com/demo@%5E1.2.3/module.js','https://user:pass@unpkg.com/demo@1.2.3/module.js','https://unpkg.com/demo@1.2.3/module.js#ignored'])expect(()=>pinnedURL(url)).toThrow();
 const polyfill='https://esm.sh/node/process.mjs';const common={[root]:module(`export * from '/node/process.mjs';`),[polyfill]:module('export const process={};')};
 expect(await freezeGraph([root],common,{},false,API)).toEqual(common);
 await expect(edges('export * from "https://other.example/unpinned.js"',root)).rejects.toThrow('pinned');
});

it('P4F04_P5F05 corrupt local dependency bytes refuse offline and explicit preparation repairs them inertly',async()=>{
 const corrupted={[root]:{body:'changed',hash:hashText('original')}};
 await expect(freezeGraph([root],{},corrupted,false,API)).rejects.toThrow('Dependency is unprepared');
 const fetch=vi.fn(async()=>new Response('export const repaired=1;'));vi.stubGlobal('fetch',fetch);
 const repaired=await freezeGraph([root],{},corrupted,true,API);expect(repaired[root]).toEqual(module('export const repaired=1;'));expect(fetch).toHaveBeenCalledTimes(1);
 expect(await freezeGraph([root],{[root]:module('common')},corrupted,false,API)).toEqual({[root]:module('common')});
});

it('P4F04_P5F05 preparation preserves UTF8 BOM bytes and hashes without executing modules',async()=>{
 const body='\ufeffglobalThis.bomCandidateExecuted=true;export const value=1;';
 vi.stubGlobal('fetch',vi.fn(async()=>new Response(new TextEncoder().encode(body))));
 const frozen=await freezeGraph([root],{},{},true,API);
 expect(frozen[root]).toEqual(module(body));expect(frozen[root].body.charCodeAt(0)).toBe(0xfeff);
 expect((globalThis as typeof globalThis&{bomCandidateExecuted?:boolean}).bomCandidateExecuted).toBeUndefined();
});
