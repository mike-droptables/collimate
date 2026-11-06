import React, {useEffect,useRef,useState} from 'https://esm.sh/react@19.3.1?target=es2022';
import {parse} from 'https://esm.sh/yaml@2.9.1?target=es2022';
import {sha256} from 'https://esm.sh/@noble/hashes@2.4.0/sha2.js?target=es2022';
import {defineRenderer,useCollimateFile,tokens} from '/api/sdk/react.js';
import {contentHash} from './package_hash.js';
import {cellPath,writeAllowed,previewPolicy,validMessage} from './policy.js';
import {frameBootstrap,mirrorTheme} from './bootstrap.js';

const MAX_BYTES=64*1024*1024;
async function readBytes(url,signal){
  const response=await fetch(url,{cache:'no-store',signal});
  if(!response.ok)throw Object.assign(new Error(`Preview file: HTTP ${response.status}`),{status:response.status});
  const reader=response.body?.getReader();if(!reader)throw new Error('Preview file has no body');
  const parts=[];let length=0;
  try{for(;;){const {done,value}=await reader.read();if(done)break;length+=value.length;if(length>MAX_BYTES)throw new Error('Preview file exceeds 64 MiB');parts.push(value);}}
  finally{await reader.cancel();}
  const bytes=new Uint8Array(length);let offset=0;for(const part of parts){bytes.set(part,offset);offset+=part.length;}return bytes;
}
/**
 * The sealed frame's document. The frame is opaque, so the host's :root tokens
 * do not reach it: the bootstrap mirrors the theme record onto the frame's
 * :root (mirrorTheme, passed in beside it), and the style imports the host's
 * font stylesheet so the --font-* stacks resolve to the bundled faces.
 */
export function frameSource(config,policy){
  const encoded=btoa(unescape(encodeURIComponent(JSON.stringify(config))));
  const bootstrap=`(${frameBootstrap.toString()})(JSON.parse(new TextDecoder().decode(Uint8Array.from(atob('${encoded}'),c=>c.charCodeAt(0)))),${mirrorTheme.toString()});`;
  return `<!doctype html><meta http-equiv="Content-Security-Policy" content="${policy}"><style>@import url(${JSON.stringify(config.fontsUrl)});html,body{margin:0;width:100%;height:100%;overflow:auto;background:var(--sheet);color:var(--ink);font-family:var(--font-ui)}</style><body><script type="module">${bootstrap}</script>`;
}
const quietButton={alignSelf:'flex-start',height:24,padding:'0 9px',borderRadius:5,border:`1px solid ${tokens.color.line2}`,background:'transparent',color:tokens.color.ink2,font:`12.5px ${tokens.font.ui}`,cursor:'pointer'};
function Preview(){
  const {api,content,isReadOnly}=useCollimateFile();
  const host=useRef(null),permission=useRef(false),active=useRef(null),sendPermission=useRef(null),prepareAction=useRef(null);
  const [enabled,setEnabled]=useState(false),[message,setMessage]=useState('Loading preview…'),[revision,setRevision]=useState(0),[needsPreparation,setNeedsPreparation]=useState(false),[preparing,setPreparing]=useState(false);
  useEffect(()=>{
    let alive=true,revoked=false,loads=0,frame=null,definitionPath='',packagePrefix='',mounted=false;
    const controller=new AbortController(),token=crypto.randomUUID(),mountId=crypto.randomUUID();
    const channel=new MessageChannel();let connected=false;
    const flushes=new Map(),requests=new Set();let flushId=1;
    permission.current=false;setEnabled(false);setNeedsPreparation(false);setPreparing(false);prepareAction.current=null;setMessage('Loading preview…');
    const signalHash=activeValue=>window.dispatchEvent(new CustomEvent('collimate:renderer-preview',{detail:{cellId:api.getCellId(),definitionPath,contentHash:hash,artifactHash,mountId,active:activeValue}}));
    let hash='',artifactHash='',frozenImports={};
    const current=()=>alive&&!revoked&&active.current===mountId&&frame?.contentWindow;
    const post=value=>{if(current()&&connected)channel.port1.postMessage({tag:'collimate-preview',...value});};
    sendPermission.current=value=>post({op:'permission',enabled:value});
    const refuse=()=>{if(alive)setMessage('preview is read-only');return false;};
    const revoke=()=>{
      revoked=true;permission.current=false;setEnabled(false);channel.port1.close();channel.port2.close();
      setMessage('Preview navigated away; permission revoked');
      if(mounted){mounted=false;signalHash(false);}
      for(const job of flushes.values()){clearTimeout(job.timer);job.reject(new Error('Preview navigated away'));}
      flushes.clear();frame?.remove();
    };
    const handle=async event=>{
      if(!current()||!connected)return;
      const request=event.data;
      if(request?.tag!=='collimate-preview'||!Number.isSafeInteger(request.id)||request.id<0)return;
      if(request.op==='depart'){revoke();return;}
      if(request.op==='flushed'){const job=flushes.get(request.id);if(job){flushes.delete(request.id);clearTimeout(job.timer);request.error?job.reject(new Error(request.error)):job.resolve();}return;}
      if(request.op==='mounted'){mounted=true;setMessage('Preview mounted');signalHash(true);return;}
      if(request.op==='error'){setMessage(String(request.message||'Preview failed').slice(0,4096));if(mounted){mounted=false;signalHash(false);}return;}
      if(request.op==='notice'){setMessage('not available in preview');return;}
      if(requests.has(request.id))return;
      if(requests.size>=128){post({op:'reply',id:request.id,error:'Preview request limit reached'});return;}
      requests.add(request.id);
      try{
        let value,urls;
        if(request.op==='read'){value=await api.getSiblingFile(cellPath(request.path));}
        else if(request.op==='paths'){value=await api.getFilePaths();}
        else if(request.op==='compile'){value=await api.preview.compile(request.files,request.entrypoint);if(Object.keys(value.imports).some(url=>!Object.hasOwn(frozenImports,url)||value.imports[url]!==frozenImports[url]))throw new Error('Dependency is outside this preview snapshot');}
        else if(request.op==='save'||request.op==='stage'){
          if(!writeAllowed(request.path,packagePrefix,permission.current,api.isReadOnly()))value=refuse();
          else if(request.op==='save'){
            if(typeof request.content!=='string'||new TextEncoder().encode(request.content).length>MAX_BYTES)throw new Error('Invalid preview write');
            value=await api.saveSiblingFile(request.path,request.content);
          }else{
            if(!(request.bytes instanceof Uint8Array)||request.bytes.byteLength>MAX_BYTES)throw new Error('Invalid preview binary write');
            value=await api.stageDraftFile?.(request.path,request.bytes)||false;
          }
          if(value)urls={[request.path]:api.getFileUrl(request.path)};
        }else throw new Error('not available in preview');
        post({op:'reply',id:request.id,value,urls});
      }catch(error){post({op:'reply',id:request.id,error:String(error?.message||error),status:error?.status});}
      finally{requests.delete(request.id);}
    };
    const connect=event=>{
      if(connected||!current()||!validMessage(event,frame,token)||event.data.op!=='connect-ready')return;
      connected=true;window.removeEventListener('message',connect);
      channel.port1.onmessage=handle;channel.port1.start();
      frame.contentWindow.postMessage({tag:'collimate-preview',token,op:'connect'},'*',[channel.port2]);
      post({op:'permission',enabled:permission.current});
    };
    active.current=mountId;window.addEventListener('message',connect);
    const offTheme=api.onThemeChange(theme=>post({op:'theme',theme}));
    const offEditing=api.onEditingChange(editing=>post({op:'editing',editing}));
    const offStyle=api.onStyleChange(style=>post({op:'style',style,fonts:api.getFontFamilies()}));
    const offFiles=api.onFilesChange(detail=>{
      if(detail.changedFiles.some(path=>path.startsWith(packagePrefix+'/'))){permission.current=false;setEnabled(false);setRevision(value=>value+1);return;}
      const urls=Object.fromEntries(detail.changedFiles.map(path=>[path,api.getFileUrl(path)]));post({op:'files',detail,urls});
    });
    const offFlush=api.onFlush?.(()=>{
      if(!current()||!connected)return;
      return new Promise((resolve,reject)=>{const id=flushId++,timer=setTimeout(()=>{flushes.delete(id);reject(new Error('Preview did not finish saving'));},4500);flushes.set(id,{resolve,reject,timer});post({op:'flush',id});});
    });
    void (async()=>{
      const pointer=JSON.parse(content);definitionPath=cellPath(pointer.renderer);const sample=cellPath(pointer.file);
      if(!definitionPath.endsWith('.renderer')||!definitionPath.includes('/'))throw new Error('Preview needs a renderer definition in a package folder');
      packagePrefix=definitionPath.slice(0,definitionPath.lastIndexOf('/'));
      const paths=await api.getFilePaths();if(paths.length>5000)throw new Error('Preview cell exceeds 5000 files');
      if(!paths.includes(definitionPath)||!paths.includes(sample))throw new Error('Preview renderer or sample file is missing');
      const files=Object.create(null);let bytes=0;
      for(const path of paths.filter(path=>path.startsWith(packagePrefix+'/'))){
        const body=await readBytes(api.getFileUrl(path),controller.signal);bytes+=body.byteLength;if(bytes>MAX_BYTES)throw new Error('Preview package exceeds 64 MiB');files[path.slice(packagePrefix.length+1)]=body;
      }
      const manifest=parse(new TextDecoder('utf-8',{fatal:true}).decode(files[definitionPath.slice(packagePrefix.length+1)]));
      const entrypoint=cellPath(manifest.entrypoint);
      const embed=manifest.embed===undefined?'iframe':manifest.embed;
      if(!['native','iframe'].includes(embed))throw new Error('Preview renderer embed must be native or iframe');
      if(!files[entrypoint])throw new Error('Renderer entrypoint is missing');
      hash=contentHash(files,sha256);
      let wrapper='__collimate_preview_entry.ts';while(Object.hasOwn(files,wrapper))wrapper='_'+wrapper;
      const wrapperCode=`import renderer from ${JSON.stringify('./'+entrypoint)}; globalThis.__collimatePreviewMount(renderer); export default renderer;`;
      const compileFiles={...files,[wrapper]:new TextEncoder().encode(wrapperCode)};
      prepareAction.current=async()=>{setPreparing(true);try{await api.preview.prepareDependencies(compileFiles,wrapper,controller.signal);if(alive&&!controller.signal.aborted)setRevision(value=>value+1);}catch(error){if(alive)setMessage(String(error?.message||error));}finally{if(alive)setPreparing(false);}};
      const compiled=await api.preview.compile(compileFiles,wrapper);
      frozenImports=compiled.imports;
      artifactHash=compiled.artifact_hash;
      if(!alive)return;
      const fontsUrl=new URL('/fonts/fonts.css',window.location.origin).href;
      const config={token,cellId:api.getCellId(),sample,code:compiled.code,runtime:compiled.runtime,imports:compiled.imports,embed,hardReadOnly:api.isReadOnly(),theme:api.getTheme(),editing:api.getEditing(),style:api.getStyle(),fonts:api.getFontFamilies(),parentOrigin:window.location.origin,fontsUrl,urls:Object.fromEntries(paths.map(path=>[path,api.getFileUrl(path)]))};
      frame=document.createElement('iframe');frame.sandbox='allow-scripts';frame.title='Sealed renderer preview';frame.style.cssText=`border:1px solid ${tokens.color.line2};border-radius:6px;flex:1;width:100%;min-height:240px;background:${tokens.color.sheet}`;
      frame.srcdoc=frameSource(config,previewPolicy(compiled.apiOrigin,fontsUrl));
      host.current?.append(frame);
      frame.addEventListener('load',()=>{
        if(++loads===1){post({op:'permission',enabled:permission.current});return;}
        // CSP cannot prevent self-navigation. The document-owned port already
        // denies replacement-document RPC; load also clears preview UI ownership.
        revoke();
      });
    })().catch(error=>{if(alive){setMessage(String(error?.message||error));setNeedsPreparation(String(error?.message||error).includes('unprepared'));}});
    return()=>{alive=false;controller.abort();permission.current=false;if(active.current===mountId){active.current=null;sendPermission.current=null;}window.removeEventListener('message',connect);channel.port1.close();channel.port2.close();offTheme();offEditing();offStyle();offFiles();offFlush?.();for(const job of flushes.values()){clearTimeout(job.timer);job.reject(new Error('Preview was replaced'));}flushes.clear();frame?.remove();if(hash)signalHash(false);};
  },[api,content,revision]);
  return <div style={{height:'100%',display:'flex',flexDirection:'column',gap:8,padding:8,boxSizing:'border-box',background:tokens.color.sheet,color:tokens.color.ink,fontFamily:tokens.font.ui,fontSize:12.5}}>
    <div style={{fontSize:12,lineHeight:1.4,color:tokens.color.ink2}}>Unapproved renderer. Preview dependencies execute offline. Prepare dependencies is the only preview action that accesses the network. Use an example file.</div>
    <div style={{display:'flex',alignItems:'center',gap:12}}><label style={{display:'inline-flex',alignItems:'center',gap:6}}><input type="checkbox" style={{accentColor:tokens.color.teal,margin:0}} checked={enabled} disabled={isReadOnly} onChange={event=>{const value=event.target.checked;permission.current=value;setEnabled(value);sendPermission.current?.(value);}}/> Allow edits for this preview</label><span role="status" style={{color:tokens.color.ink3}}>{message}</span></div>
    {needsPreparation&&<button style={{...quietButton,opacity:preparing?0.6:1}} disabled={preparing} onClick={()=>{void prepareAction.current?.();}}>{preparing?'Preparing dependencies…':'Prepare dependencies'}</button>}
    <div ref={host} style={{display:'flex',flex:1,minHeight:240}}/>
  </div>;
}
export default defineRenderer({component:Preview});
