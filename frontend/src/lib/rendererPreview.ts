/** Read-only preview primitives; unapproved code is returned, never imported. */
import {getApiUrl} from './api';
import {compileRendererFiles, type CompiledBundle} from './rendererBundle';
import {edges, freezeGraph, graphImports, hashText, readPrepared, writePrepared, type FrozenGraph} from './previewDependencies';
const MAX_PACKAGE_BYTES=64*1024*1024;
const MAX_RUNTIME_BYTES=2*1024*1024;
export interface PreviewCompilation extends CompiledBundle {
  runtime: {react:string;session:string}; apiOrigin:string; imports:Record<string,string>; artifact_hash:string;
}
let activeCompiles=0;
const runtimes=new Map<string,Promise<{react:string;session:string;common:FrozenGraph}>>();
async function runtimeTexts(base:string) {
  let pending=runtimes.get(base);
  if(!pending){
    pending=Promise.all(['react','session','preview-common'].map(async name=>{
      const response=await fetch(`${base}/sdk/${name === 'preview-common' ? name+'.json' : name+'.js'}`,{signal:AbortSignal.timeout(30_000)});
      if(!response.ok)throw new Error(`Preview runtime ${name}: HTTP ${response.status}`);
      const reader=response.body?.getReader();
      if(!reader)throw new Error('Preview runtime response has no body');
      const chunks:Uint8Array[]=[];let total=0;
      try {for(;;){const {done,value}=await reader.read();if(done)break;total+=value.byteLength;if(total>MAX_RUNTIME_BYTES)throw new Error('Preview runtime exceeds two MiB');chunks.push(value);}}
      finally {await reader.cancel();}
      const bytes=new Uint8Array(total);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.byteLength;}
      return new TextDecoder('utf-8',{fatal:true}).decode(bytes);
    })).then(([react,session,common])=>({react,session,common:JSON.parse(common) as FrozenGraph}));
    runtimes.set(base,pending);
    while(runtimes.size>4)runtimes.delete(runtimes.keys().next().value!);
    void pending.catch(()=>{if(runtimes.get(base)===pending)runtimes.delete(base);});
  }
  return pending;
}
export async function compilePreview(files:Record<string,Uint8Array>,entrypoint:string,prepare=false,signal?:AbortSignal):Promise<PreviewCompilation>{
  if(!files||typeof files!=='object'||Array.isArray(files)||typeof entrypoint!=='string')throw new Error('Invalid preview package');
  const entries=Object.entries(files);let size=0;
  if(entries.length>5000)throw new Error('Preview package exceeds 5000 files');
  for(const [path,bytes] of entries){if(path.length>2048||!(bytes instanceof Uint8Array))throw new Error('Invalid preview file');size+=bytes.byteLength;}
  if(size>MAX_PACKAGE_BYTES)throw new Error('Preview package exceeds 64 MiB');
  if(activeCompiles>=4)throw new Error('Preview compiler is busy; retry');
  activeCompiles++;
  try {
    const base=new URL(getApiUrl().replace(/\/$/,'')+'/',window.location.href).href.replace(/\/$/,'');
    const [compiled,runtime]=await Promise.all([compileRendererFiles(files,entrypoint),runtimeTexts(base)]);
    const parsed=await edges(compiled.code);
    const roots=parsed.imports.concat((await edges(runtime.react)).imports,(await edges(runtime.session)).imports,Object.keys(runtime.common));
    const key=base+'\n'+compiled.source_hash;
    const cached=await readPrepared(key);
    const graph=await freezeGraph(roots,runtime.common,cached,prepare,base,signal);
    if(signal?.aborted)throw new Error('Preview preparation was revoked');
    if(prepare)await writePrepared(key,graph);
    const imports=await graphImports(graph);
    const artifact_hash=hashText(JSON.stringify([compiled.source_hash,compiled.code,parsed.body,runtime.react,runtime.session,Object.entries(graph).sort(([a],[b])=>a.localeCompare(b))]));
    return {...compiled,code:parsed.body,runtime:{react:runtime.react.replace(/\bimport\s*\(/g,'globalThis.__collimatePreviewImport('),session:runtime.session},imports,artifact_hash,apiOrigin:new URL(base).origin};
  }finally{activeCompiles--;}
}
export async function rendererFilePaths(channel:string,cellId:string,versionId?:string):Promise<string[]>{
  if(!channel||!cellId)throw new Error('Renderer has no cell context');
  const query=versionId?`?version_id=${encodeURIComponent(versionId)}`:'';
  const response=await fetch(`${getApiUrl()}/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/file-paths${query}`,{cache:'no-store'});
  if(!response.ok)throw new Error(`Renderer file paths: HTTP ${response.status}`);
  const value:unknown=await response.json();
  const paths=(value as {paths?:unknown})?.paths;
  if(!Array.isArray(paths)||!paths.every(path=>typeof path==='string'))throw new Error('Invalid renderer file paths');
  return paths;
}

/** Explicit online action; all fetched modules remain inert bytes. */
export async function preparePreviewDependencies(files:Record<string,Uint8Array>,entrypoint:string,signal?:AbortSignal):Promise<void>{await compilePreview(files,entrypoint,true,signal?AbortSignal.any([signal,AbortSignal.timeout(30_000)]):AbortSignal.timeout(30_000));}
