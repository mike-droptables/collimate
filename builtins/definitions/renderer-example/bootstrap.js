/**
 * Serialized into the opaque frame beside frameBootstrap, which receives it as
 * its second argument. Keep it entirely self-contained. It writes the host's
 * theme record onto the frame's :root the way the host's sandbox does for an
 * iframe renderer: every token by its CSS name ('--sheet-2'), color-scheme,
 * data-theme and data-scheme. A token the previous record wrote and the new
 * one lacks is removed, so a skin change never leaves a stale token behind;
 * custom properties the previewed renderer set on :root itself (milkdown's
 * --colmd-zoom, say) are never touched.
 */
export function mirrorTheme(root, theme, previous) {
  const tokens=value=>Object.entries(value&&typeof value.vars==='object'&&value.vars?value.vars:{}).filter(([name,token])=>/^--[A-Za-z0-9_-]+$/.test(name)&&typeof token==='string');
  const next=new Map(tokens(theme));
  const style=root.style;
  for(const [name] of tokens(previous))if(!next.has(name))style.removeProperty(name);
  for(const [name,value] of next)style.setProperty(name,value);
  const scheme=theme&&theme.scheme==='light'?'light':'dark';
  style.setProperty('color-scheme',scheme);
  root.setAttribute('data-theme',theme&&typeof theme.themeId==='string'&&theme.themeId?theme.themeId:'unknown');
  root.setAttribute('data-scheme',scheme);
}

/** Serialized into the opaque frame. Keep this function entirely self-contained. */
export async function frameBootstrap(config, mirrorTheme) {
  const pending=new Map(), writes=new Set(), flushers=new Set(), fileListeners=new Set(), themeListeners=new Set(), editingListeners=new Set(), styleListeners=new Set();
  let next=1, theme=config.theme, editable=false, rendererObject=null, cleanup=null, generation=0;
  mirrorTheme(document.documentElement,theme);
  // The user's editing preferences, on this document's <html> as the host
  // sets them on an iframe renderer's: editables that leave spellcheck unset
  // inherit it.
  const readEditing=value=>({spellcheck:!(value&&value.spellcheck===false)});
  let editing=readEditing(config.editing);
  document.documentElement.setAttribute('spellcheck',String(editing.spellcheck));
  // The style the host keeps for the previewing renderer (theme, font, text
  // size) and its font catalogue, relayed as they change. A renderer under
  // development may set it to try its own control: that applies here only
  // and is never posted, so a preview never writes a shipped renderer's
  // style.
  const readStyle=value=>({
    theme:typeof value?.theme==='string'?value.theme:null,
    font:typeof value?.font==='string'?value.font:null,
    textSize:typeof value?.textSize==='number'&&Number.isFinite(value.textSize)?value.textSize:null,
  });
  let style=readStyle(config.style), fontFamilies=Array.isArray(config.fonts)?config.fonts:[];
  // Transfer once before importing any untrusted module. A MessagePort belongs
  // to this document and cannot be recovered by a replacement document from a
  // copied URL token or the frame's surviving WindowProxy.
  const port=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{window.removeEventListener('message',connect);reject(new Error('Preview channel timed out'));},10000);
    const connect=event=>{
      if(event.source!==parent||event.origin!==config.parentOrigin||event.data?.tag!=='collimate-preview'||event.data.op!=='connect'||event.data.token!==config.token||event.ports.length!==1)return;
      clearTimeout(timer);window.removeEventListener('message',connect);resolve(event.ports[0]);
    };
    window.addEventListener('message',connect);
    parent.postMessage({tag:'collimate-preview',token:config.token,id:0,op:'connect-ready'},'*');
  });
  const post=value=>port.postMessage({tag:'collimate-preview',...value});
  window.addEventListener('pagehide',()=>post({id:0,op:'depart'}));
  const request=(op,args={})=>new Promise((resolve,reject)=>{
    if(pending.size>=128){reject(new Error('Preview request limit reached'));return;}
    const id=next++,timer=setTimeout(()=>{pending.delete(id);reject(new Error('Preview request timed out'));},35000);
    pending.set(id,{resolve,reject,timer});post({id,op,...args});
  });
  const write=(op,args)=>{const promise=request(op,args);writes.add(promise);void promise.then(()=>writes.delete(promise),()=>writes.delete(promise));return promise;};
  const subscribe=(set,callback)=>{set.add(callback);return()=>set.delete(callback);};
  const unavailable=()=>{post({id:0,op:'notice',message:'not available in preview'});};
  const api={
    getContent:()=>request('read',{path:config.sample}),
    getDiffContent:async()=>null,getDiffLabels:()=>null,
    getFilename:()=>config.sample,getCellId:()=>config.cellId,
    getSiblingFile:path=>request('read',{path}),
    getFilePaths:()=>request('paths'),
    getSurface:async()=>null,
    onSurfaceChange:()=>()=>{},
    preview:{prepareDependencies:async()=>{throw new Error('not available in preview');},
      compile:(files,entrypoint)=>request('compile',{files,entrypoint})},
    getFileUrl:path=>Object.hasOwn(config.urls,path)?config.urls[path]:'',
    reportChange:content=>{void write('save',{path:config.sample,content}).catch(()=>{});},
    saveSiblingFile:(path,content)=>write('save',{path,content}).then(Boolean,()=>false),
    stageDraftFile:async(path,bytes)=>{try{const raw=typeof bytes==='string'?new TextEncoder().encode(bytes):bytes instanceof Blob?new Uint8Array(await bytes.arrayBuffer()):bytes instanceof ArrayBuffer?new Uint8Array(bytes):bytes;return !!await write('stage',{path,bytes:raw});}catch{return false;}},
    isReadOnly:()=>config.hardReadOnly||!editable,
    onFilesChange:callback=>subscribe(fileListeners,callback),
    onThemeChange:callback=>subscribe(themeListeners,callback),getTheme:()=>theme,
    onEditingChange:callback=>{const off=subscribe(editingListeners,callback);callback(editing);return off;},getEditing:()=>editing,
    onStyleChange:callback=>{const off=subscribe(styleListeners,callback);callback(style);return off;},getStyle:()=>style,
    setStyle:patch=>{style=readStyle({...style,...(patch&&typeof patch==='object'?patch:{})});for(const callback of styleListeners)callback(style);},
    getFontFamilies:()=>fontFamilies,
    onFlush:callback=>subscribe(flushers,callback),
    onSetContent:()=>{unavailable();return()=>{};},
    getVisible:()=>{unavailable();return false;},onVisibleChange:()=>{unavailable();return()=>{};},
    getFocused:()=>{unavailable();return false;},onFocusChange:()=>{unavailable();return()=>{};},
    emitAction:async()=>{unavailable();return false;},revealFile:unavailable,
    desktop:{capabilities:{},openExternal:async()=>{unavailable();},mountGuest:()=>{unavailable();throw new Error('not available in preview');}},
    injectCss:css=>{const style=document.createElement('style');style.textContent=String(css);styleRoot.append(style);},
    loadCss:href=>new Promise((resolve,reject)=>{const link=document.createElement('link');link.rel='stylesheet';link.href=href;link.onload=resolve;link.onerror=()=>reject(new Error('Preview stylesheet is blocked or unavailable'));styleRoot.append(link);}),
    ready:()=>{},
  };
  port.onmessage=async event=>{
    if(event.data?.tag!=='collimate-preview')return;
    const message=event.data;
    if(message.op==='reply'){
      const job=pending.get(message.id);if(!job)return;pending.delete(message.id);clearTimeout(job.timer);
      if(message.error)job.reject(Object.assign(new Error(message.error),{status:message.status}));else{if(message.urls)Object.assign(config.urls,message.urls);job.resolve(message.value);}return;
    }
    if(message.op==='permission'){if(editable!==!!message.enabled){editable=!!message.enabled;if(rendererObject)await mountRenderer();}}
    if(message.op==='theme'){const previous=theme;theme=message.theme;mirrorTheme(document.documentElement,theme,previous);for(const callback of themeListeners)callback(theme);}
    if(message.op==='editing'){editing=readEditing(message.editing);document.documentElement.setAttribute('spellcheck',String(editing.spellcheck));for(const callback of editingListeners)callback(editing);}
    if(message.op==='style'){style=readStyle(message.style);if(Array.isArray(message.fonts))fontFamilies=message.fonts;for(const callback of styleListeners)callback(style);}
    if(message.op==='files'){Object.assign(config.urls,message.urls);for(const callback of fileListeners)callback(message.detail);}
    if(message.op==='flush'){
      try{await Promise.all([...flushers].map(callback=>Promise.resolve().then(callback)));await Promise.all([...writes]);post({id:message.id,op:'flushed'});}
      catch(error){post({id:message.id,op:'flushed',error:String(error?.message||error)});}
    }
  };
  port.start();
  globalThis.__collimatePreviewImport=(url,base)=>{if(typeof url!=='string')return Promise.reject(new Error('Module is outside this preview snapshot'));if(base)url=new URL(url,base).href;if(!Object.hasOwn(config.imports,url))return Promise.reject(new Error('Module is outside this preview snapshot'));return import(url);};
  const dataURL=text=>'data:text/javascript;base64,'+btoa(unescape(encodeURIComponent(text)));
  const map=document.createElement('script');map.type='importmap';map.textContent=JSON.stringify({imports:{...config.imports,'/api/sdk/react.js':dataURL(config.runtime.react),'/api/sdk/session.js':dataURL(config.runtime.session)}});document.head.append(map);
  const host=document.createElement('div');host.style.cssText='height:100%;width:100%';document.body.append(host);
  const styleRoot=config.embed==='native'?host.attachShadow({mode:'open'}):host;
  const mount=document.createElement('div');mount.style.cssText='height:100%;width:100%';styleRoot.append(mount);
  const mountRenderer=async()=>{
    const own=++generation;
    try{cleanup?.();cleanup=null;mount.replaceChildren();const nextCleanup=await rendererObject.mount(mount,api);if(own!==generation){if(typeof nextCleanup==='function')nextCleanup();return;}cleanup=typeof nextCleanup==='function'?nextCleanup:null;post({id:0,op:'mounted'});}
    catch(error){post({id:0,op:'error',message:String(error?.message||error)});}
  };
  globalThis.__collimatePreviewMount=async renderer=>{
    if(!renderer||typeof renderer.mount!=='function'){post({id:0,op:'error',message:'Preview renderer needs a default mount export'});return;}
    rendererObject=renderer;await mountRenderer();
  };
  window.addEventListener('error',event=>post({id:0,op:'error',message:event.message||'Preview failed'}));
  window.addEventListener('unhandledrejection',event=>post({id:0,op:'error',message:String(event.reason?.message||event.reason)}));
  const module=document.createElement('script');module.type='module';module.textContent=config.code;module.onerror=()=>post({id:0,op:'error',message:'Preview module failed to link'});document.body.append(module);
}
