import {isRunPath} from './runningCell';
import {compilePreview, preparePreviewDependencies, rendererFilePaths} from './rendererPreview';
import {answerRendererBundleRequest} from './rendererBundle';
import {currentChannelId} from './currentChannel';
import {saveSiblingFileInCell} from './siblingWrite';
import {revealCellFile} from './revealFile';
import {buildForwardedCombos} from './keybindings/forwardedCombos';
import {readThemeSnapshot} from './themeSnapshot';
import {readEditingSnapshot} from './editingSnapshot';
import {readRendererStyle,rendererFontFamilies} from './rendererStyles';
import {useRendererStylesStore} from '@/stores/useRendererStylesStore';
import {rendererSurface} from './rendererSurface';
import {editCellDocument,forgetCellDocumentView,getFileDocument,snapshotCellDocument} from './fileDocuments';
import type {DocumentViewEdit,DocumentViewSnapshot} from './fileDocument';
import {readRendererSiblingText} from './fileCommands';
import {useCollimateStore} from '@/stores/useCollimateStore';
import type {RendererRecoverySnapshot, RendererSnapshotCodec} from './fileSnapshotCodec';
export interface SandboxHostContext {
  target: Window; isCurrent: () => boolean; ready: () => void;
  rendererId: string; rendererHash?: string; fileName: string;
  getDocument?:()=>Partial<DocumentViewSnapshot>&{content:string;diffContent:string|null}; onContentChange?:(content:string,view?:DocumentViewEdit)=>void;
  getContent?:()=>Promise<string>;
  registerSnapshotCodec?:(codec:RendererSnapshotCodec)=>Promise<void>;
  onSnapshotChange?:(snapshot:RendererRecoverySnapshot,view?:DocumentViewEdit)=>void;
  channel?: string; cellId?: string; versionId?: string; readOnly?: boolean;
  /** The frame's renderer subscribed (or withdrew) api.onAnnotationRequest. */
  onAnnotationCapable?: (capable: boolean) => void;
}
const physicalViews=new WeakMap<Window,{viewId:string;forget:()=>void}>();
/** Called after the physical iframe is removed from message dispatch, never on parking. */
export function disposeSandboxView(target:Window):void {
  const view=physicalViews.get(target);physicalViews.delete(target);view?.forget();
}
function admittedView(target:Window,data:{viewId?:unknown;viewSeq?:unknown}):DocumentViewEdit|null {
  const view=physicalViews.get(target);
  return view&&view.viewId===data.viewId&&Number.isSafeInteger(data.viewSeq)&&Number(data.viewSeq)>0
    ?{viewId:view.viewId,viewSeq:Number(data.viewSeq)}:null;
}
const preparationJobs=new WeakMap<Window,Map<number,AbortController>>();
export function handleSandboxHostMessage(event: MessageEvent, context: SandboxHostContext): void {
  const {target,isCurrent}=context;
  if(event.origin!==window.location.origin || event.source!==target || !isCurrent()) return;
  answerRendererBundleRequest(event,context.rendererId,context.rendererHash,target,isCurrent);
  const data=event.data;
  const post=(message: unknown)=>{if(isCurrent())target.postMessage(message,window.location.origin);};
  switch(data?.type) {
    case 'GET_SURFACE': void rendererSurface(context.channel || currentChannelId(),context.cellId || '',context.fileName,context.versionId).then(
      value=>post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,value}),
      error=>post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,error:String(error?.message || error)}));break;
    case 'FILE_PATHS': void rendererFilePaths(context.channel || currentChannelId(),context.cellId || '',context.versionId).then(
      paths=>post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,value:paths}),
      error=>post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,error:String(error?.message || error)}));break;
    case 'CANCEL_PREPARE_RENDERER_DEPENDENCIES': preparationJobs.get(target)?.get(data.id)?.abort();break;
    case 'PREPARE_RENDERER_DEPENDENCIES': {
      let jobs=preparationJobs.get(target);if(!jobs){jobs=new Map();preparationJobs.set(target,jobs);}
      if(jobs.has(data.id))break;
      const controller=new AbortController();jobs.set(data.id,controller);
      const ownerCheck=setInterval(()=>{if(!isCurrent())controller.abort();},100);
      void preparePreviewDependencies(data.files,data.entrypoint,controller.signal).then(
        ()=>post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,value:null}),
        error=>post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,error:String(error?.message || error)})).finally(()=>{clearInterval(ownerCheck);jobs.delete(data.id);});break;
    }
    case 'COMPILE_RENDERER': void compilePreview(data.files,data.entrypoint).then(
      value=>post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,value}),
      error=>post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,error:String(error?.message || error)}));break;
    case 'RENDERER_READY': context.ready(); break;
    // Whether the frame's renderer takes annotation requests (sandbox.html
    // posts it on subscribe and withdrawal, and again on every PING_READY so a
    // host re-acquiring a pooled frame learns it). ANNOTATION_ACK and
    // ANNOTATION_RESULT never come here: requestSandboxAnnotation
    // (lib/rendererAnnotation) listens for its own, like FLUSH_DRAFT_ACK.
    case 'ANNOTATION_CAPABLE': context.onAnnotationCapable?.(data.capable===true);break;
    case 'GET_DOCUMENT': {
      if(typeof data.viewId!=='string'||!data.viewId||data.viewId.length>128){post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,error:'A physical renderer view identity is required.'});break;}
      const previous=physicalViews.get(target);
      if(previous&&previous.viewId!==data.viewId){post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,error:'Renderer view identity changed without physical disposal.'});break;}
      if(!previous){const channel=context.channel||currentChannelId(),cellId=context.cellId??'',path=context.fileName;
        const owner=cellId?getFileDocument(channel,cellId,path):undefined,viewId=data.viewId;
        physicalViews.set(target,{viewId,forget:()=>owner?owner.forgetView(viewId):forgetCellDocumentView(channel,cellId,path,viewId)});}
      const state=useCollimateStore.getState();const files=(state.cellCache[context.cellId??'']??state.locateCell(context.cellId??'')?.cell)?.files??[];post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,value:{...context.getDocument?.(),readOnly:!!context.readOnly||!!context.versionId||isRunPath(context.fileName),canInteract:!context.readOnly&&!context.versionId,files}});break;}
    case 'GET_DOCUMENT_CONTENT': void (context.getContent?.() ?? Promise.resolve(context.getDocument?.().content ?? '')).then(
      value=>post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,value}),
      error=>post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,error:String(error?.message || error)}));break;
    case 'REGISTER_SNAPSHOT_CODEC': {
      const task=context.registerSnapshotCodec?.(data.codec) ?? Promise.reject(new Error('Snapshot codecs are unavailable in this view.'));
      void task.then(()=>post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,value:null}),
        error=>post({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,error:String(error?.message || error)}));break;
    }
    case 'FILE_SNAPSHOT_UPDATED': {const view=admittedView(target,data);if(view&&!context.readOnly&&!context.versionId&&!isRunPath(context.fileName)){
      if(context.onSnapshotChange)context.onSnapshotChange(data.snapshot,view);
      else if(context.cellId)void snapshotCellDocument(context.channel||currentChannelId(),context.cellId,context.fileName,data.snapshot,view,()=>isCurrent()&&physicalViews.get(target)?.viewId===view.viewId).catch(error=>console.error('Document snapshot failed',error));
    }break;}
    case 'FILE_UPDATED': {const view=admittedView(target,data);if(view&&!context.readOnly && !isRunPath(context.fileName) && !context.versionId && typeof data.content==='string'){
      if(context.onContentChange)context.onContentChange(data.content,view);
      else if(context.cellId)void editCellDocument(context.channel||currentChannelId(),context.cellId,context.fileName,data.content,view,()=>isCurrent()&&physicalViews.get(target)?.viewId===view.viewId).catch(error=>console.error('Document edit failed',error));
    }break;}
    case 'FORWARD_KEYDOWN': case 'FORWARD_KEYUP': window.dispatchEvent(new KeyboardEvent(data.type==='FORWARD_KEYUP'?'keyup':'keydown',{key:data.key,code:data.code,altKey:data.altKey,ctrlKey:data.ctrlKey,shiftKey:data.shiftKey,metaKey:data.metaKey,bubbles:true,cancelable:true}));break;
    case 'REVEAL_FILE': revealCellFile(context.cellId,data.path,{newTab:data.newTab===true});break;
    // An OS file drag entered the window over this frame (sandbox.html §3c),
    // where the host's own window listeners (useCanvasFileDrop) never see
    // it: raise the same flag, so the renderers go pointer-passive and the
    // drag's next move reaches the canvas.
    case 'OS_FILE_DRAG': useCollimateStore.getState().setOsFileDragActive(true);break;
    case 'SIBLING_READ': void readRendererSiblingText(context.channel || currentChannelId(),context.cellId || '',data.path,context.versionId).then(content=>post({type:'SIBLING_READ_RESULT',id:data.id,content})).catch(error=>post({type:'SIBLING_READ_RESULT',id:data.id,error:error.message,status:error.status}));break;
    // The renderer's style control (api.setStyle): a user preference, not
    // file content, so read-only views take it too. It always lands under
    // this frame's own renderer id, never an id the message names.
    case 'SET_STYLE_PATCH': if(data.patch&&typeof data.patch==='object'&&!Array.isArray(data.patch)){
      const patch:Record<string,unknown>={};
      for(const key of ['theme','font','textSize'] as const) if(Object.prototype.hasOwnProperty.call(data.patch,key)) patch[key]=data.patch[key];
      void useRendererStylesStore.getState().commitStyle(context.rendererId,patch).catch(error=>console.warn('[renderer style] save failed',error));
    }break;
    case 'SIBLING_WRITE': void saveSiblingFileInCell(context.cellId,data.path,data.content,context.readOnly||!!context.versionId).then(({ok,reason})=>post({type:'SIBLING_WRITE_RESULT',id:data.id,ok,reason}));break;
  }
}
/** The host's active theme: the sandbox mirrors its variables onto its own
 *  :root and fires the renderer's api.onThemeChange subscribers. */
export function pushSandboxTheme(target: Window): void {
  target.postMessage({type:'SET_THEME',...readThemeSnapshot()},window.location.origin);
}
/** The editing preferences (spellcheck): the sandbox applies them to its
 *  own <html>, which the renderer's editables inherit. */
export function pushSandboxEditing(target: Window): void {
  target.postMessage({type:'SET_EDITING',...readEditingSnapshot()},window.location.origin);
}
/** This renderer's style choices (its record in the channel's
 *  renderer_styles section, all null for defaults) and the font catalogue
 *  its style control offers. */
export function pushSandboxStyle(target: Window, rendererId: string): void {
  target.postMessage({type:'SET_STYLE',style:readRendererStyle(rendererId),fonts:rendererFontFamilies()},window.location.origin);
}
export function pushSandboxKeys(target: Window): void {
  target.postMessage({type:'SET_KEYBINDINGS',combos:buildForwardedCombos()},window.location.origin);
}
