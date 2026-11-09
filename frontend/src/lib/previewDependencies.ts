/** Inert ESM graph preparation. Parsing and hashing never import candidate code. */
import {parse} from 'es-module-lexer/minimal/js';
import {sha256} from '@noble/hashes/sha2.js';
import {mirrorUrl} from './moduleMirror';
export interface FrozenModule {body:string;hash:string}
export type FrozenGraph=Record<string,FrozenModule>;
export const hashText=(body:string)=>Array.from(sha256(new TextEncoder().encode(body)),b=>b.toString(16).padStart(2,'0')).join('');
const MAX_BYTES=64*1024*1024,MAX_MODULES=512;
function moduleURL(value:string):URL {
 const url=new URL(value);
 if(url.protocol!=='https:'||url.username||url.password||url.hash)throw new Error(`Invalid preview dependency URL: ${value}`);
 return url;
}
export function pinnedURL(value:string):string {
 const url=moduleURL(value),path=decodeURIComponent(url.pathname);
 const version=/(?:@|\/v?)\d+\.\d+\.\d+(?:[-+][\w.-]+)?(?:\/|$)/;
 const immutable=/(?:^|\/)(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})(?:\/|$)/;
 if(/[~^*]|@(?:latest|next|canary|stable)(?:[/?&]|$)/.test(decodeURIComponent(url.pathname+url.search))||(!version.test(path)&&!immutable.test(path)))throw new Error(`Preview dependency needs a version or immutable hash pinned HTTPS URL: ${value}`);
 return url.href;
}
export async function edges(body:string,base?:string):Promise<{body:string;imports:string[]}> {
 const [imports]=parse(body);let rewritten=body;const paths:string[]=[];
 for(const item of [...imports].reverse()){
  if(item.d===-2)continue;
  if(item.n===undefined){const importer=base?`((url)=>globalThis.__collimatePreviewImport(url,${JSON.stringify(base)}))`:'globalThis.__collimatePreviewImport';rewritten=rewritten.slice(0,item.ss)+importer+rewritten.slice(item.ss+6);continue;}
  let name=item.n;
  if(base){const resolved=moduleURL(new URL(name,base).href);name=resolved.origin===new URL(base).origin?resolved.href:pinnedURL(resolved.href);}
  else if(!['/api/sdk/react.js','/api/sdk/session.js'].includes(name))name=pinnedURL(name);
  paths.push(name);
  if(base){const start=item.d>=0?item.s:item.s-1,end=item.d>=0?item.e:item.e+1;rewritten=rewritten.slice(0,start)+JSON.stringify(name)+rewritten.slice(end);}
 }
 return {body:rewritten,imports:paths};
}
export async function verifyGraph(graph:FrozenGraph):Promise<void>{
 if(!graph||typeof graph!=='object'||Array.isArray(graph)||Object.keys(graph).length>MAX_MODULES)throw new Error('Invalid prepared dependency graph');
 let size=0;
 for(const [url,module]of Object.entries(graph)){moduleURL(url);if(typeof module?.body!=='string'||module.hash!==hashText(module.body))throw new Error('Prepared dependency hash differs; prepare dependencies again');size+=new TextEncoder().encode(module.body).length;}
 if(size>MAX_BYTES)throw new Error('Prepared dependencies exceed 64 MiB');
}
/** Freeze the graph under `roots`; module-CDN bodies are fetched from the app's mirror under `apiBase`, keyed by their upstream URL. */
export async function freezeGraph(roots:string[],common:FrozenGraph,cached:FrozenGraph,online:boolean,apiBase:string,signal?:AbortSignal):Promise<FrozenGraph>{
 await verifyGraph(common);let snapshotProblem='';try{await verifyGraph(cached);}catch(error){snapshotProblem=String((error as Error).message);cached={};}const graph:FrozenGraph={};let size=0;
 const visit=async(url:string):Promise<void>=>{
  if(url.startsWith('/api/sdk/'))return;
  moduleURL(url);if(Object.hasOwn(graph,url))return;
  if(Object.keys(graph).length>=MAX_MODULES)throw new Error('Prepared dependencies exceed 512 modules');
  let module=common[url]??cached[url];
  if(!module){
   if(!online)throw new Error(`Dependency is unprepared: ${url}. ${snapshotProblem} Choose Prepare dependencies (the only preview action that accesses the network).`);
   const response=await fetch(mirrorUrl(url,apiBase),{credentials:'omit',redirect:'error',signal:signal??AbortSignal.timeout(30_000)});
   if(!response.ok)throw new Error(`Prepare dependencies: HTTP ${response.status}`);
   const reader=response.body?.getReader();if(!reader)throw new Error('Dependency response has no body');
   const chunks:Uint8Array[]=[];let length=0;
   try{for(;;){const {done,value}=await reader.read();if(done)break;length+=value.length;if(length+size>MAX_BYTES)throw new Error('Prepared dependencies exceed 64 MiB');chunks.push(value);}}finally{await reader.cancel();}
   const bytes=new Uint8Array(length);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
   const body=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bytes);module={body,hash:hashText(body)};
  }
  graph[url]={...module};size+=new TextEncoder().encode(module.body).length;if(size>MAX_BYTES)throw new Error('Prepared dependencies exceed 64 MiB');
  for(const child of(await edges(module.body,url)).imports)await visit(child);
 };
 for(const root of roots){if(!root.startsWith('/api/sdk/'))pinnedURL(root);await visit(root);}return graph;
}
export async function graphImports(graph:FrozenGraph):Promise<Record<string,string>>{
 const imports:Record<string,string>={};
 for(const [url,module]of Object.entries(graph)){const rewritten=await edges(module.body,url);imports[url]='data:text/javascript;base64,'+btoa(unescape(encodeURIComponent(rewritten.body)));}
 return imports;
}
export async function readPrepared(key:string):Promise<FrozenGraph>{return storage(key);}
export async function writePrepared(key:string,graph:FrozenGraph):Promise<void>{await verifyGraph(graph);await storage(key,graph);}
async function storage(key:string,value?:FrozenGraph):Promise<FrozenGraph>{
 const database=await new Promise<IDBDatabase>((resolve,reject)=>{const request=indexedDB.open('collimate-preview-dependencies',1);request.onupgradeneeded=()=>request.result.createObjectStore('graphs');request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});
 try{return await new Promise((resolve,reject)=>{const tx=database.transaction('graphs',value?'readwrite':'readonly');const request=value?tx.objectStore('graphs').put(value,key):tx.objectStore('graphs').get(key);request.onerror=()=>reject(request.error);tx.oncomplete=()=>resolve(value??request.result??{});tx.onabort=()=>reject(tx.error);});}finally{database.close();}
}
