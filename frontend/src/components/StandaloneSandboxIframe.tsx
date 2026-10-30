import type {DocumentViewEdit,DocumentViewSnapshot} from '@/lib/fileDocument';
import {registerSnapshotCodec,type RendererDocumentSnapshot,type RendererRecoverySnapshot} from '@/lib/fileSnapshotCodec';
import {isRunPath} from '@/lib/runningCell';
import {subscribeSurface} from '@/lib/rendererSurface';
import {useRendererFilename} from '@/lib/rendererAliases';
import {currentChannelId} from '@/lib/currentChannel';
import {handleSandboxHostMessage, disposeSandboxView, pushSandboxTheme, pushSandboxEditing, pushSandboxStyle, pushSandboxKeys} from '@/lib/sandboxBridge';
import {EDITING_CHANGED_EVENT} from '@/lib/editingSnapshot';
import {RENDERER_STYLES_CHANGED_EVENT, type RendererStylesChangedDetail} from '@/lib/rendererStyles';
import {registerRendererFlusher, flushSandbox} from '@/lib/rendererFlush';
import {registerAnnotationResponder, notifyAnnotationResponders, requestSandboxAnnotation, selectedTextInFrame, AnnotationUnavailableError} from '@/lib/rendererAnnotation';
/**
 * StandaloneSandboxIframe — a non-pooled sandbox iframe for the fullscreen
 * file view.
 *
 * The normal in-canvas renderer path ({@link SandboxIframe}) borrows its
 * <iframe> from the {@link GuestPoolContext} warm pool. Those pooled iframes
 * live inside a world-space overlay that is a child of the canvas pan/zoom
 * container, and are positioned over their placeholder's screen rect by an
 * offset-math sync loop. That overlay sits at the canvas's stacking level — so
 * a body-level fullscreen overlay (rendered above the whole canvas) would
 * occlude the pooled iframe entirely, and the renderer would never be visible.
 *
 * For the (single, short-lived) fullscreen view we therefore mount our own
 * <iframe> directly, filling the host. It loads the exact same sandbox.html
 * shell + renderer as the pooled path and speaks the same postMessage bridge
 * (RENDERER_READY / FILE_UPDATED / FORWARD_KEYDOWN / SIBLING_READ /
 * SIBLING_WRITE / FILES_CHANGED + theme + keybinding pushes), so editing,
 * theming, and shortcuts behave identically. Pooling/LRU/world-space
 * positioning are intentionally dropped — there is only ever one of these
 * alive at a time. No separate scroll transport is needed,
 * and a single fullscreen instance has no sibling pane to sync with.
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { onForwardedCombosChange } from '@/lib/keybindings/forwardedCombos';
import { buildSandboxSrc } from '@/lib/sandboxSrc';

interface StandaloneSandboxIframeProps {
  fileHash: string;
  documentId?: string;
  fileContent?: string;
  diffContent?: string;
  onContentChange?: (content:string,view?:DocumentViewEdit)=>void;
  documentSnapshot?:RendererDocumentSnapshot&{ownerId?:string;viewAcks?:DocumentViewSnapshot['viewAcks']};
  onSnapshotChange?:(snapshot:RendererRecoverySnapshot,view?:DocumentViewEdit)=>void;
  onGetContent?:()=>Promise<string>;
  fileName: string;
  rendererId: string;
  rendererHash?: string;
  isActiveFocus: boolean;
  diffHash?: string;
  /** Names for the diff's two sides (e.g. "v3"/"v4") — forwarded to
   *  sandbox.html as query params for api.getDiffLabels(). */
  diffLabels?: { base: string; current: string };
  cellId?: string;
  /** Exact immutable snapshot when mounted by History. */
  versionId?: string;
  channel?: string;
  /** Hard host lock (review cell, diff view) — forwarded to sandbox.html as
   *  `readonly=1` so the renderer's api.isReadOnly() tells the truth. */
  readOnly?: boolean;
  /** SandboxIframe's pool-eviction hook, accepted for the shared host prop
   *  surface (FullFileEditor's IframeRenderer). Never called here: this
   *  frame is not pooled, so nothing evicts it. */
  onDocumentRecreated?: () => boolean;
}

export const StandaloneSandboxIframe: React.FC<StandaloneSandboxIframeProps> = ({
  fileHash,
  documentId,
  fileContent,
  diffContent,
  onContentChange,documentSnapshot,onSnapshotChange,onGetContent,
  fileName,
  rendererId,
  rendererHash,
  isActiveFocus,
  diffHash,
  diffLabels,
  cellId,
  versionId,
  channel,
  readOnly,
}) => {
  const displayName = useRendererFilename(fileName);
  const contentRef=useRef(fileContent);contentRef.current=fileContent;
  const diffRef=useRef(diffContent);diffRef.current=diffContent;
  const changeRef=useRef(onContentChange);changeRef.current=onContentChange;
  const snapshotRef=useRef(documentSnapshot);snapshotRef.current=documentSnapshot;
  const snapshotChange=useRef(onSnapshotChange);snapshotChange.current=onSnapshotChange;
  const contentGetter=useRef(onGetContent);contentGetter.current=onGetContent;

  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [isReady, setIsReady] = useState(false);
  // The annotation responder's probes: this view's focus, and whether the
  // frame's renderer subscribed a handler (ANNOTATION_CAPABLE).
  const focusRef = useRef(isActiveFocus);
  const capableRef = useRef(false);
  // True once the overlay's fade-out has finished, so its infinite spinner
  // animation stops ticking under an invisible parent. Reset on re-cover.
  const [spinnerParked, setSpinnerParked] = useState(false);
  if (!isReady && spinnerParked) setSpinnerParked(false);

  // Same sandbox.html URL contract as the pooled SandboxIframe (shared builder),
  // so the renderer inside loads exactly as it would in the canvas.
  const sandboxSrc = useMemo(
    () => buildSandboxSrc({ rendererId, rendererHash, fileHash: documentId ? '' : fileHash, documentId, diffHash, diffLabels, fileName, displayName, cellId, versionId, channel: channel || currentChannelId(), readOnly:documentId?false:readOnly }),
    [rendererId, rendererHash, documentId ? '' : fileHash, documentId, diffHash, diffLabels, fileName, displayName, cellId, versionId, channel, readOnly],
  );

  // New src (file/renderer/diff change) → re-cover with the spinner until the
  // fresh content reports ready.
  useEffect(() => { setIsReady(false); }, [sandboxSrc]);
  // A new frame has said nothing yet about annotation requests.
  useEffect(() => { capableRef.current = false; notifyAnnotationResponders(); }, [sandboxSrc]);
  useEffect(() => { focusRef.current = isActiveFocus; notifyAnnotationResponders(); }, [isActiveFocus]);

  // Safety-net: drop the overlay after 3s even if the renderer never signals
  // api.ready() (older renderers, or one that crashes silently), mirroring the
  // pooled path so the user is never stuck behind the spinner.
  useEffect(() => {
    if (isReady) return;
    const t = setTimeout(() => setIsReady(true), 3000);
    return () => clearTimeout(t);
  }, [isReady, sandboxSrc]);

  useEffect(()=>{const target=iframeRef.current?.contentWindow;return()=>{if(target)disposeSandboxView(target);};},[sandboxSrc]);

  // postMessage bridge — scoped to our own iframe's window. Mirrors the
  // SandboxIframe handlers that matter for a standalone view: readiness,
  // edits (→ collimate:file-updated so the editor's auto-save runs), key
  // forwarding (so host shortcuts like double-tap Esc work from inside the
  // renderer), and sibling-file fetches.
  useEffect(() => {
    let alive = true;
    const handleMessage = (e: MessageEvent) => {
      const target=iframeRef.current?.contentWindow;
      if(target)handleSandboxHostMessage(e,{target,isCurrent:()=>alive && iframeRef.current?.contentWindow===target,
        ready:()=>setIsReady(true),rendererId,rendererHash,fileName,channel,cellId,versionId,readOnly:readOnly||!!diffHash,getDocument:()=>({...snapshotRef.current,content:contentRef.current??'',diffContent:diffRef.current??null}),getContent:()=>contentGetter.current?contentGetter.current():Promise.resolve(contentRef.current??''),registerSnapshotCodec,onSnapshotChange:(snapshot,view)=>snapshotChange.current?.(snapshot,view),onContentChange:(body,view)=>changeRef.current?.(body,view),
        onAnnotationCapable:(capable)=>{capableRef.current=capable;notifyAnnotationResponders();}});
    };
    const unregisterFlush=registerRendererFlusher(cellId,fileName,()=>{
      const target=iframeRef.current?.contentWindow;
      return target ? flushSandbox(target,()=>alive && iframeRef.current?.contentWindow===target) : undefined;
    });
    // This view as an annotation responder (lib/rendererAnnotation); the
    // fullscreen view is the only one of its kind, so it is always visible.
    const unregisterAnnotation = cellId ? registerAnnotationResponder({
      cellId, path: fileName, rendererId, viewKey: `standalone:${sandboxSrc}`,
      visible: () => alive,
      focused: () => alive && focusRef.current,
      capable: () => alive && capableRef.current,
      canInteract: () => !readOnly && !versionId && !diffHash,
      selection: () => (alive ? selectedTextInFrame(iframeRef.current?.contentWindow) : ''),
      request: (request, signal) => {
        const target = iframeRef.current?.contentWindow;
        return target
          ? requestSandboxAnnotation(target, request, signal, () => alive && iframeRef.current?.contentWindow === target)
          : Promise.reject(new AnnotationUnavailableError('parked', 'The file’s view is not loaded.'));
      },
    }) : () => {};
    window.addEventListener('message', handleMessage);
    return () => { alive = false; unregisterFlush(); unregisterAnnotation(); window.removeEventListener('message', handleMessage); };
  }, [fileName, channel, cellId, versionId, readOnly, diffHash, rendererId, rendererHash, sandboxSrc]);

  useEffect(()=>{
    if(isReady && fileContent!==undefined)iframeRef.current?.contentWindow?.postMessage({type:'SET_DOCUMENT',...documentSnapshot,content:fileContent,readOnly:!!readOnly||!!versionId||!!diffHash||isRunPath(fileName),canInteract:!readOnly&&!versionId&&!diffHash},window.location.origin);
  },[isReady,fileContent,readOnly,versionId,diffHash,fileName,documentSnapshot]);

  // FILES_CHANGED forwarding. The pooled path gets this from the guest pool's
  // cell-scoped fan-out; a standalone iframe isn't in the pool, so without
  // this a renderer in the fullscreen view never learns its cell's files
  // changed — including the sidecar it just wrote itself, or one an agent
  // wrote while the user was reading.
  useEffect(() => {
    if (!cellId) return;
    const onFilesChange = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.cellId !== cellId) return;
      iframeRef.current?.contentWindow?.postMessage(
        {
          type: 'FILES_CHANGED',
          files: detail.files,
          cellId,
          changedFiles: detail.changedFiles,
        },
        window.location.origin,
      );
    };
    window.addEventListener('collimate:files-changed-broadcast', onFilesChange);
    return () => window.removeEventListener('collimate:files-changed-broadcast', onFilesChange);
  }, [cellId]);

  useEffect(() => {
    if(versionId || !cellId)return;
    return subscribeSurface(channel || currentChannelId(),cellId,fileName,()=>{
      iframeRef.current?.contentWindow?.postMessage(
        {type:'SURFACE_CHANGED',cellId,path:fileName},window.location.origin);
    });
  },[channel,cellId,fileName,versionId]);

  // Push theme, editing preferences + keybindings into the iframe once it's
  // ready, and re-push on theme change / editing change / remap — same
  // contract sandbox.html expects from the pooled consumer so Monaco/Crepe/…
  // render with the host theme, spellcheck and the renderer's style choices
  // and forward combos.
  useEffect(() => {
    if (!isReady) return;
    const cw = iframeRef.current?.contentWindow;
    if (!cw) return;
    const postTheme = () => pushSandboxTheme(cw);
    const postKeys = () => {
      pushSandboxKeys(cw);
    };
    const postEditing = () => pushSandboxEditing(cw);
    const postStyle = () => pushSandboxStyle(cw, rendererId);
    const onStyleChanged = (e: Event) => {
      if ((e as CustomEvent<RendererStylesChangedDetail>).detail?.rendererId === rendererId) postStyle();
    };
    postTheme();
    postEditing();
    postStyle();
    postKeys();
    window.addEventListener('collimate:theme-changed', postTheme);
    window.addEventListener(EDITING_CHANGED_EVENT, postEditing);
    window.addEventListener(RENDERER_STYLES_CHANGED_EVENT, onStyleChanged);
    const unsub = onForwardedCombosChange(postKeys);
    return () => {
      window.removeEventListener('collimate:theme-changed', postTheme);
      window.removeEventListener(EDITING_CHANGED_EVENT, postEditing);
      window.removeEventListener(RENDERER_STYLES_CHANGED_EVENT, onStyleChanged);
      unsub();
    };
  }, [isReady, rendererId]);

  return (
    <div data-standalone-renderer className="w-full h-full relative">
      <iframe key={sandboxSrc}
        ref={iframeRef}
        src={sandboxSrc}
        onLoad={e => { const cw = e.currentTarget.contentWindow; if (cw) { pushSandboxTheme(cw); pushSandboxEditing(cw); pushSandboxStyle(cw, rendererId); } }}
        title={fileName}
        className="w-full h-full block border-0"
        sandbox="allow-scripts allow-same-origin allow-forms"
        style={{ pointerEvents: isActiveFocus ? 'auto' : 'none' }}
      />
      {/* Fade-out spinner overlay until the renderer's first paint lands. */}
      <div
        className="absolute inset-0 z-20 bg-sheet flex items-center justify-center pointer-events-none"
        style={{ opacity: isReady ? 0 : 1, transition: 'opacity 380ms ease-out' }}
        onTransitionEnd={(e) => { if (e.target === e.currentTarget && isReady) setSpinnerParked(true); }}
      >
        {!spinnerParked && (
          <div className="w-4 h-4 rounded-full border-2 border-teal/30 border-t-teal animate-spin" />
        )}
      </div>
    </div>
  );
};
