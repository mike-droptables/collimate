import {registerSnapshotCodec,type RendererDocumentSnapshot,type RendererRecoverySnapshot} from '@/lib/fileSnapshotCodec';
import { adoptScrollbars, installScrollbars } from '@/lib/scrollbars';
import {isRunPath} from '@/lib/runningCell';
import {rendererSurface,subscribeSurface} from '@/lib/rendererSurface';
import {useRendererFilename,rendererRealPath,rendererChangedFiles} from '@/lib/rendererAliases';
import {compilePreview, preparePreviewDependencies, rendererFilePaths} from '@/lib/rendererPreview';
import {registerRendererFlusher, boundedFlush} from '@/lib/rendererFlush';
import {
  registerAnnotationResponder, notifyAnnotationResponders, selectedTextIn, AnnotationUnavailableError,
  type AnnotationHandler, type AnnotationRequest,
} from '@/lib/rendererAnnotation';
/**
 * NativeRenderer — runs trusted renderers directly in the main document
 * inside a Shadow DOM container for CSS isolation.
 *
 * The renderer gets the same unified `api` object as iframe-based renderers,
 * but implemented with direct JS calls instead of postMessage.
 */

import React, { useEffect, useLayoutEffect, useRef, useCallback, useState } from 'react';
import { btn, btnPrimary } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { getCachedRenderer } from '@/lib/nativeRendererCache';
import { getApiUrl, cellFileUrl, uploadBlob } from '@/lib/api';
import { mirrorUrl } from '@/lib/moduleMirror';
import { currentChannelId } from '@/lib/currentChannel';
import { cellFileBase, cellFileCap } from '@/lib/cellFileCap';
import { readThemeSnapshot, type ThemeSnapshot } from '@/lib/themeSnapshot';
import { EDITING_CHANGED_EVENT, readEditingSnapshot, type EditingSnapshot } from '@/lib/editingSnapshot';
import {
  RENDERER_STYLES_CHANGED_EVENT, readRendererStyle, rendererFontFamilies, type RendererStylesChangedDetail,
} from '@/lib/rendererStyles';
import type { RendererStyleRecord } from '@/lib/api';
import { useRendererStylesStore } from '@/stores/useRendererStylesStore';
import {
  isDesktop,
  desktopCaps,
  hasCap,
  openExternal,
  type RendererDesktopSurface,
  type RendererGuestOptions,
  type GuestHandle,
} from '@/lib/desktop';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { useRendererConcealed } from '@/contexts/RendererConcealContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { guestPoolId, mountGuestInto, type HostGuestHandle } from '@/components/guestMount';
import { revealCellFile } from '@/lib/revealFile';
import { editCellDocument,snapshotCellDocument,getFileDocument,forgetCellDocumentView } from '@/lib/fileDocuments';
import type { DocumentViewEdit } from '@/lib/fileDocument';
import { readRendererSiblingText, writeCellBlob } from '@/lib/fileCommands';
import { saveSiblingFileInCell } from '@/lib/siblingWrite';
import { registerRendererInstance } from '@/lib/rendererInputContainment';
import { attachScrimWheel } from '@/lib/wheelReplay';
import { afterScrimPress, focusShadowAt } from '@/lib/selectionFocus';

interface NativeRendererProps {
  documentSnapshot?:RendererDocumentSnapshot & {ownerId?:string;viewAcks?:Readonly<Record<string,number>>};
  onSnapshotChange?:(snapshot:RendererRecoverySnapshot,view?:DocumentViewEdit)=>void;
  onGetContent?:()=>Promise<string>;
  onContentChange?: (content: string,view?:DocumentViewEdit) => void;
  rendererId: string;
  rendererHash?: string;
  fileContent: string;
  fileName: string;
  cellId?: string;
  /** Exact immutable snapshot when mounted by History. */
  versionId?: string;
  channel?: string;
  isActiveFocus: boolean;
  isDiffMode?: boolean;
  diffContent?: string;
  /** Human names for the diff's two sides (e.g. "v3"/"v4"), read by
   *  renderers via api.getDiffLabels() to title their diff panes. Changes
   *  arrive together with diffContent/cellId (same scrub step), which
   *  already force a remount — so labels live in a ref, not mount deps. */
  diffLabels?: { base: string; current: string };
  /** False while the cell is scrolled offscreen and the host has flipped
   *  its subtree to display:none (which clears GPU/canvas drawing buffers).
   *  Surfaced to the renderer via api.onVisibleChange so GPU-backed
   *  renderers can repaint when shown again. Defaults to true for mount
   *  sites that are always visible (full editor, draft preview). */
  visible?: boolean;
  /** Hard host lock (review cell, diff view) — surfaced as api.isReadOnly()
   *  so a renderer can refuse to offer editing at all, instead of the host
   *  silently discarding saves it invited. The iframe hosts carry the same
   *  flag through the sandbox URL's `readonly=1`. */
  readOnly?: boolean;
  /** Captured persistence owner during bounded native disposal. */
  onDisposeContent?: (content: string) => void | Promise<void>;
}

export const NativeRenderer: React.FC<NativeRendererProps> = ({
  rendererId,
  rendererHash,
  fileContent,
  fileName,
  cellId,
  versionId,
  channel,
  isActiveFocus,
  isDiffMode,
  diffContent,
  diffLabels,
  visible = true,
  readOnly: requestedReadOnly,
  onDisposeContent,
  onContentChange,
  documentSnapshot,onSnapshotChange,onGetContent,
}) => {
  const readOnly=!!requestedReadOnly||!!versionId||!!isDiffMode||isRunPath(fileName);
  const displayName = useRendererFilename(fileName);
  const hostRef = useRef<HTMLDivElement>(null);
  const documentOwnerId=documentSnapshot?.ownerId;
  const identity=JSON.stringify([rendererId,rendererHash,fileName,displayName,cellId,versionId,channel,isDiffMode,documentOwnerId]);
  const latestIdentity=useRef(identity);latestIdentity.current=identity;
  const disposalSave=useRef(onDisposeContent ?? onContentChange);disposalSave.current=onDisposeContent ?? onContentChange;
  const changeOwner=useRef(onContentChange);changeOwner.current=onContentChange;
  const snapshotOwner=useRef(onSnapshotChange);snapshotOwner.current=onSnapshotChange;
  const exactContent=useRef(onGetContent);exactContent.current=onGetContent;
  const snapshotRef=useRef(documentSnapshot);snapshotRef.current=documentSnapshot;
  const snapshotCallbacks=useRef(new Set<(snapshot:RendererDocumentSnapshot)=>void>());
  const shadowRef = useRef<ShadowRoot | null>(null);
  const scrollActivityRef = useRef<(() => void) | null>(null);
  useEffect(() => () => { scrollActivityRef.current?.(); scrollActivityRef.current = null; }, []);
  const cleanupRef = useRef<(() => void) | null>(null);
  const readyFallbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Fade-in: masks the brief unstyled flash while .mount() pulls in the
  // renderer module and applies styles into the shadow root.
  // The overlay lifts when the renderer signals api.ready() (preferred) or
  // when the safety-net timeout fires (for renderers that don't opt in yet).
  const [isReady, setIsReady] = useState(false);
  // True once the overlay's fade-out has finished: its spinner children
  // (infinite CSS animations) are then unmounted so they stop ticking under
  // an invisible parent. Reset whenever the overlay re-covers.
  const [spinnerParked, setSpinnerParked] = useState(false);
  if (!isReady && spinnerParked) setSpinnerParked(false);
  // Load/mount failure, rendered as an in-component error panel (with Retry
  // and a plain-text fallback) instead of raw DOM injected into the shadow
  // root. `attempt` is a mount-effect dep: bumping it re-runs the whole load
  // (the renderer cache evicts failed entries, so a retry re-fetches).
  const [mountError, setMountError] = useState<string | null>(null);
  const [showPlainText, setShowPlainText] = useState(false);
  const [attempt, setAttempt] = useState(0);

  const fileContentRef = useRef(fileContent);
  const diffContentRef = useRef(diffContent);
  const diffLabelsRef = useRef(diffLabels);
  // Live visibility for the api closure (read by api.getVisible) plus the
  // set of renderer-side subscribers (api.onVisibleChange). Refs so they
  // survive the mount effect re-running and are reachable from the
  // separate visibility effect below without rebuilding the shadow DOM.
  const visibleRef = useRef(visible);
  const visibilityCbsRef = useRef<Set<(v: boolean) => void>>(new Set());
  // Cell-focus mirror for the api closure (api.getFocused / onFocusChange).
  // A native renderer lives in the main document, so a window-level input
  // listener it installs would hear every keystroke app-wide; the platform
  // contract is that renderers gate global input handlers on this signal so
  // an unfocused cell can never see (or swallow) input meant elsewhere.
  const focusRef = useRef(isActiveFocus);
  const focusCbsRef = useRef<Set<(f: boolean) => void>>(new Set());
  // Unregister handle for this instance's input-containment scope.
  const containmentUnregRef = useRef<(() => void) | null>(null);
  // Same reason: api.isReadOnly() must answer with the CURRENT lock state
  // (a cell can become a review cell while its renderer stays mounted), and
  // the api object is built once inside the mount effect.
  const readOnlyRef = useRef(readOnly);
  const interaction=!requestedReadOnly&&!versionId&&!isDiffMode;
  const interactionRef=useRef(interaction);
  interactionRef.current=interaction;
  // canInteract gates this view's annotation responder too.
  useEffect(()=>{notifyAnnotationResponders();},[interaction]);
  // The guest pool, for api.desktop.mountGuest. The context object has a
  // stable identity, but the mount effect below deliberately doesn't list
  // it — a pool swap would mean a whole new canvas, and by then this
  // renderer has remounted anyway.
  const pool = useGuestPool();
  // Guests this renderer has mounted, hoisted to a ref so the conceal
  // effect below can reach the live set without re-running the mount.
  const mountedGuestsRef = useRef<HostGuestHandle[]>([]);
  // The guest half of cell conceal. A guest's pixels live in a native
  // view (or its placeholder in the world-space overlay) — the cell's
  // burn/dissolve/drag-hide opacity can't reach either, so the conceal
  // context is forwarded by hand, exactly like SandboxIframe does for
  // pooled iframes. Offscreen (`visible === false`, subtree
  // display:none'd) counts too: the container has no honest rect then.
  const concealed = useRendererConcealed();
  const osFileDragActive = useCollimateStore(s => s.osFileDragActive);
  const guestsConcealed = concealed || !visible;
  const guestsConcealedRef = useRef(guestsConcealed);
  useEffect(() => {
    guestsConcealedRef.current = guestsConcealed;
    for (const handle of mountedGuestsRef.current) {
      try { handle.setConcealed(guestsConcealed); } catch (err) { console.error('guest conceal failed:', err); }
    }
  }, [guestsConcealed]);

  // Layout synchronization precedes passive mount cleanup. Retain the exact
  // subscriber owner so a replacement identity (or retry) cannot notify the
  // retiring renderer before that cleanup has emptied its callbacks.
  const setContentOwnerRef = useRef<{
    identity: string; attempt: number; viewId:string; viewSeq:number;
    callbacks: Array<(content: string) => void>;
  } | null>(null);

  // Update refs so API calls always get the latest content without remounting.
  // When the fileContent PROP genuinely changes (not the initial value, not an
  // echo of the renderer's own reportChange, which already advanced the ref), notify
  // onSetContent subscribers too: the files-changed broadcast fires renderer
  // callbacks BEFORE this sync runs, so a callback that read api.getContent()
  // saw the stale ref — this delivers the fresh content as soon as it lands.
  // Synchronize during commit: a passive effect can run after the renderer
  // has already emitted a newer edit and would echo the older prop over it.
  useLayoutEffect(() => {
    diffContentRef.current = diffContent;
    diffLabelsRef.current = diffLabels;
    readOnlyRef.current = readOnly;
    interactionRef.current=!requestedReadOnly&&!versionId&&!isDiffMode;
    const owner = setContentOwnerRef.current;
    const snapshot=documentSnapshot??{content:fileContent,editSeq:0,materializedSeq:0};
    const sameOwner=owner?.identity===identity && owner.attempt===attempt;
    // A committed React snapshot can precede a synchronous edit from this
    // mounted view. Only the ACK captured with that snapshot proves it has
    // observed every emission; permissions still update while it catches up.
    if(sameOwner && documentSnapshot && (documentSnapshot.viewAcks?.[owner.viewId]??0)<owner.viewSeq)return;
    const changed = fileContentRef.current !== fileContent;
    fileContentRef.current = fileContent;
    if(sameOwner)for(const callback of snapshotCallbacks.current)callback(snapshot);
    if (changed && snapshot.editSeq===snapshot.materializedSeq && sameOwner) {
      for (const cb of [...owner.callbacks]) {
        try { cb(fileContent); } catch (err) { console.error('native renderer set-content error:', err); }
      }
    }
  }, [fileContent, diffContent, diffLabels, readOnly, identity, attempt,documentSnapshot]);

  // Visibility lifecycle. A lightweight signal, deliberately NOT a remount:
  // visibility flips constantly as cells scroll offscreen, and rebuilding
  // the shadow DOM each time would defeat the renderer-module cache and drop
  // imperative state. The host flips `visible` (false when the cell's
  // subtree is display:none'd offscreen, which clears GPU/canvas drawing
  // buffers); we update the ref the api reads and notify subscribers so
  // GPU-backed renderers can repaint. Kept out of the mount effect's deps
  // so it never touches the shadow DOM.
  useEffect(() => {
    visibleRef.current = visible;
    for (const cb of Array.from(visibilityCbsRef.current)) {
      try { cb(visible); } catch (err) { console.error('native renderer visibility error:', err); }
    }
    notifyAnnotationResponders();
  }, [visible]);

  // Same shape for cell focus: update the ref the api reads and notify
  // subscribers. Kept out of the mount effect's deps — focus flips must
  // never remount the renderer.
  useEffect(() => {
    focusRef.current = isActiveFocus;
    for (const cb of Array.from(focusCbsRef.current)) {
      try { cb(isActiveFocus); } catch (err) { console.error('native renderer focus error:', err); }
    }
    notifyAnnotationResponders();
  }, [isActiveFocus]);

  const handleContentChange = useCallback((content: string,view?:DocumentViewEdit,isCurrent?:()=>boolean) => {
    if(readOnlyRef.current || versionId)return;
    if(changeOwner.current){changeOwner.current(content,view);return;}
    if(cellId)void editCellDocument(channel || currentChannelId(),cellId,fileName,content,view,isCurrent).catch(error=>console.error('Document edit failed',error));
  }, [fileName, cellId, channel, versionId]);

  // First-click replay: the focus scrim eats the click that focuses the cell,
  // so the user always had to click twice. Record the click point, let the
  // focus change land (two rAFs — one for the state commit, one for the
  // paint that flips the host's pointer-events), then re-dispatch a synthetic
  // click at the same point inside the shadow root so the first click both
  // focuses AND acts. Deliberately does not stop propagation — the bubble is
  // what triggers the focus change in the first place.
  const handleScrimClick = useCallback((e: React.MouseEvent) => {
    const { clientX, clientY } = e;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const shadow = shadowRef.current;
      if (!shadow) return;
      const target = shadow.elementFromPoint(clientX, clientY);
      if (!target) return;
      target.dispatchEvent(new MouseEvent('click', {
        bubbles: true,
        composed: true,
        cancelable: true,
        clientX,
        clientY,
        view: window,
      }));
    }));
  }, []);

  // First-press focus: a synthetic click focuses nothing, so the editable
  // under the press takes the keyboard itself (the replayed click still
  // lands on it), unless something else took the keyboard meanwhile.
  const handleScrimPress = useCallback((e: React.PointerEvent) => {
    if (e.button !== 0) return;
    afterScrimPress(e.clientX, e.clientY, (x, y) => {
      const shadow = shadowRef.current;
      if (shadow) focusShadowAt(shadow, x, y);
    });
  }, []);

  // First-wheel replay, the click's sibling: a vertical wheel over the scrim
  // focuses the cell (StackItem, ContentPane), and its travel is replayed
  // into the shadow scroller under the pointer once the host's
  // pointer-events has flipped (the same two rAFs — elementFromPoint skips
  // the host until then). The scrim cancels that wheel's native scroll,
  // which would otherwise land in the flipped host as well and scroll the
  // first notch twice (attachScrimWheel). The replay animates from where
  // the content stood and owns the burst: every further notch over this
  // wrapper (the scrim's parent) folds into that animation until it
  // settles, so none is lost to the hand-off; afterwards wheels land
  // natively. Propagation is kept: the bubble selects.
  const scrimWheelRef = useCallback((scrim: HTMLDivElement | null) => (scrim
    ? attachScrimWheel(scrim, () => shadowRef.current,
      (run) => requestAnimationFrame(() => requestAnimationFrame(run)))
    : undefined), []);

  // Reset ready state whenever the mount inputs change — we're about
  // to rebuild the shadow DOM and need the overlay to re-cover until the
  // new mount paints. diffContent is included so switching the diff
  // target (which replaces the content of the target file) also forces
  // the renderer to remount and re-fetch — the renderer reads diff
  // content once on mount, so without this the old diff stuck around.
  useEffect(() => {
    setIsReady(false);
    // A new mount target also clears any stale failure panel (retry state is
    // reset by the Retry button itself, which is why `attempt` is absent).
    setMountError(null);
    setShowPlainText(false);
  }, [rendererId, rendererHash, fileName, displayName, cellId, versionId, channel, isDiffMode, diffContent,documentOwnerId]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    // Create shadow DOM once
    if (!shadowRef.current) {
      shadowRef.current = host.attachShadow({ mode: 'open' });
      // The workbench's scrollbars (lib/scrollbars): neither the sheet nor a
      // scroll event crosses the shadow boundary, so the renderer's scroll
      // containers take the sheet from here, and their activity mark and
      // thumb drag from listeners on this root, for as long as the host lives.
      adoptScrollbars(shadowRef.current);
      scrollActivityRef.current = installScrollbars(shadowRef.current);
    }
    const shadow = shadowRef.current;

    // Clear previous mount
    if (cleanupRef.current) {
      try { cleanupRef.current(); } catch { /* safety */ }
      cleanupRef.current = null;
    }
    shadow.innerHTML = '';

    const mountEl = document.createElement('div');
    mountEl.id = 'mount';
    mountEl.style.cssText = 'width:100%;height:100%;';
    shadow.appendChild(mountEl);

    const documentSnapshotCbs=new Set<(snapshot:RendererDocumentSnapshot)=>void>();
    snapshotCallbacks.current=documentSnapshotCbs;
    const filesChangedCbs: Array<(detail: unknown) => void> = [];
    const surfaceChangedCbs = new Set<() => void>();
    // onSetContent subscribers, notified by the prop-sync effect when the
    // host's fileContent prop changes, and api.onFlush subscribers (flush
    // requests). A renderer that doesn't subscribe simply never reacts.
    const setContentCbs: Array<(content: string) => void> = [];
    const setContentOwner = { identity, attempt, viewId:crypto.randomUUID(),viewSeq:0,callbacks: setContentCbs };
    setContentOwnerRef.current = setContentOwner;
    const flushCbs: Array<() => void | Promise<void>> = [];
    // The renderer's api.onAnnotationRequest handler (one per view) and the
    // requests it is answering, aborted when it unsubscribes or unmounts.
    let annotationHandler: AnnotationHandler | null = null;
    const annotationJobs = new Set<AbortController>();

    // Per-run liveness: false once this run's cleanup has started. Every
    // async continuation (module load, renderer mount, ready(), the fallback
    // timer, late guests) checks it, so a superseded or unmounted run can
    // neither paint over a newer mount nor store a cleanup no one calls.
    let alive = true;
    let disposing = false;
    let persistDisposal: ((content:string)=>void)|undefined;
    const disposalWrites:Promise<void>[]=[];
    const mountedIdentity=identity;
    // Component-lifetime subscriber Sets (never reassigned), emptied in the cleanup.
    const visibilityCbs = visibilityCbsRef.current;
    const focusCbs = focusCbsRef.current;

    // Guests this renderer mounted, so a renderer that forgets to destroy
    // one doesn't leak an OS process past its own unmount. Shared with the
    // conceal effect through mountedGuestsRef.
    const mountedGuests: HostGuestHandle[] = [];
    mountedGuestsRef.current = mountedGuests;
    // Per-mount sequence, so a renderer that mounts more than one guest
    // gets a distinct pool key for each. Deterministic rather than random:
    // a renderer that mounts its guests in the same order on every mount
    // re-acquires the same pool entries, which is what lets a keepAlive
    // guest survive a remount of the renderer around it.
    let guestSeq = 0;

    // Desktop-shell surface, undefined in a browser. Only NATIVE renderers
    // get it: they run in the host's own document and are installed code,
    // whereas the iframe path deliberately sandboxes untrusted renderer
    // source — handing that a shell handle would punch through the
    // boundary the sandbox exists to draw. Renderers read it via
    // useDesktop(api), whose undefined case covers browser and iframe
    // alike.
    // Omitted, not stubbed, when the shell can't host guests: a renderer
    // feature-tests with `api.desktop?.mountGuest` and needs a straight
    // answer.
    const mountGuest = hasCap('nativeGuests')
      ? (hostEl: HTMLElement, options: RendererGuestOptions): GuestHandle => {
          const handle = mountGuestInto(pool, hostEl, {
            // fileName is part of the key because one cell can host the same
            // renderer for two different files at once (two tabs, or two split
            // leaves). Without it both hosts mint the same poolId, the second
            // acquire steals the entry's container, and the loser's release is
            // guarded out — one guest ping-ponging between two hosts. The same
            // file never appears twice in a cell (one file = at most one tab),
            // so cell+renderer+file+seq is collision-proof.
            poolId: guestPoolId(cellId, rendererId, fileName, guestSeq++),
            src: options.src,
            // Default partition: one storage jar per renderer per cell.
            // Per-renderer so two generators' pages can't read each
            // other's cookies, and per-cell so the same renderer on two
            // cells doesn't share a login the user set up on one of
            // them. A renderer that wants to share state across its
            // cells names its own partition.
            partition: options.partition ?? `${rendererId}-${cellId ?? 'nocell'}`,
            ...(options.device !== undefined ? { device: options.device } : {}),
            ...(options.keepAlive !== undefined ? { keepAlive: options.keepAlive } : {}),
            ...(options.interactive !== undefined ? { interactive: options.interactive } : {}),
            // The pool key above is deliberately stable across remounts —
            // that is what reattaches a parked guest. A renderer that can
            // tell one session from the next says so here, and the pool
            // replaces the parked guest instead of reattaching it.
            ...(options.guestSessionKey !== undefined ? { guestSessionKey: options.guestSessionKey } : {}),
            ...(cellId !== undefined ? { cellId } : {}),
          });
          // A dead run's late guest: its cleanup already drained mountedGuests.
          if (!alive) {
            try { handle.destroy(); } catch (err) { console.error('guest destroy failed:', err); }
            return handle;
          }
          mountedGuests.push(handle);
          // Sync conceal unconditionally: the pool entry may be REUSED
          // from a mount that died mid-conceal (a cell dropped onto a
          // stack remounts its renderer while drag-hidden), and a stale
          // concealed flag would keep the guest invisible forever. And a
          // guest mounted mid-conceal must start hidden with its host,
          // not painted over the animation.
          handle.setConcealed(guestsConcealedRef.current);
          return handle;
        }
      : undefined;

    const desktopSurface: RendererDesktopSurface | undefined = isDesktop() ? {
      capabilities: desktopCaps(),
      openExternal,
      ...(mountGuest ? { mountGuest } : {}),
    } : undefined;

    // Where api.getFileUrl points: the cell's capability URL once
    // cellFileCap resolves (awaited with the module below). With no channel
    // or no cell there are no cell files: no cap is minted, and the plain
    // shape 404s.
    const fileChannel = channel || currentChannelId();
    const fileCell = cellId || '';
    // The same document may be retargeted before this mount retires. Keep
    // its owner object so disposal can release this view after a rename.
    const mountedDocumentOwner=cellId?getFileDocument(fileChannel,fileCell,fileName):undefined;
    let fileBase = cellFileUrl(fileChannel, fileCell, '', versionId);

    // Build the native API object (direct calls, no postMessage)
    const api = {
      getContent: async () => exactContent.current?exactContent.current():fileContentRef.current,
      registerSnapshotCodec,
      saveSnapshot:(snapshot:RendererRecoverySnapshot)=>{
        if(!alive||readOnlyRef.current)return;
        const view={viewId:setContentOwner.viewId,viewSeq:++setContentOwner.viewSeq};
        if(snapshotOwner.current)snapshotOwner.current(snapshot,view);
        else if(cellId)void snapshotCellDocument(fileChannel,fileCell,fileName,snapshot,view,()=>alive).catch(error=>console.error('Document snapshot failed',error));
      },
      getDocumentSnapshot:async()=>(!versionId&&!readOnlyRef.current&&cellId?getFileDocument(fileChannel,fileCell,fileName)?.getDocumentSnapshot():undefined)??snapshotRef.current??{content:fileContentRef.current,editSeq:0,materializedSeq:0},
      onDocumentSnapshot:(callback:(snapshot:RendererDocumentSnapshot)=>void)=>{if(!alive)return()=>{};documentSnapshotCbs.add(callback);return()=>{documentSnapshotCbs.delete(callback);};},
      getDiffContent: async () => diffContentRef.current ?? null,
      // Names for the diff's two sides (e.g. "v3"/"v4"); null when the host
      // has no meaningful names and the renderer should use its own generic
      // labels ("Before"/"After").
      getDiffLabels: () => diffLabelsRef.current ?? null,
      getFilename: () => displayName,
      getCellId: () => cellId ?? '',
      getSurface: () => rendererSurface(fileChannel,fileCell,fileName,versionId),
      onSurfaceChange: (callback:()=>void) => {
        surfaceChangedCbs.add(callback);
        return()=>{surfaceChangedCbs.delete(callback);};
      },
      getFilePaths: () => rendererFilePaths(fileChannel,fileCell,versionId),
      preview: {compile: compilePreview, prepareDependencies: preparePreviewDependencies},
      getSiblingFile: async (path: string): Promise<string> => {
        return readRendererSiblingText(channel || currentChannelId(), cellId || '', rendererRealPath(fileName,displayName,path), versionId);
      },
      // Built on the cell's file capability (lib/cellFileCap), resolved
      // before the renderer mounts, so a URL it hands its own opaque-origin
      // preview frame is one the server answers there too.
      getFileUrl: (path: string): string => {
        if(path==='')return fileBase;
        const realPath=rendererRealPath(fileName,displayName,path);
        const url=`${fileBase}${realPath.split('/').map(encodeURIComponent).join('/')}`;
        if(versionId)return url;
        const state=useCollimateStore.getState();
        const file=(state.cellCache[fileCell]??state.locateCell(fileCell)?.cell)?.files.find(file=>file.name===realPath);
        if(!file)throw new Error(`File ${realPath} is unavailable.`);
        return `${url}?revision=${file.file_revision}`;
      },
      // Advance the ref so the host feeding this value back as the
      // fileContent prop is not echoed to onSetContent subscribers.
      reportChange: (content: string) => {
        if(disposing){persistDisposal?.(content);return;}
        if (!alive || readOnlyRef.current) return;
        fileContentRef.current = content;
        handleContentChange(content,{viewId:setContentOwner.viewId,viewSeq:++setContentOwner.viewSeq},()=>alive);
      },
      onFilesChange: (cb: (detail: unknown) => void) => {
        filesChangedCbs.push(cb);
        return () => {
          const i = filesChangedCbs.indexOf(cb);
          if (i >= 0) filesChangedCbs.splice(i, 1);
        };
      },
      // Fires when the host's fileContent prop changes (a commit or a
      // files-changed refresh) without remounting. Subscribing renderers
      // apply it to their editor state and must NOT re-fire reportChange (the
      // host already owns the value it pushed).
      onSetContent: (cb: (content: string) => void) => {
        setContentCbs.push(cb);
        return () => {
          const i = setContentCbs.indexOf(cb);
          if (i >= 0) setContentCbs.splice(i, 1);
        };
      },
// File operations use pinned bodies and conditional durable receipts.
      onFlush: (cb: () => void | Promise<void>) => {
        flushCbs.push(cb);
        return () => {
          const i = flushCbs.indexOf(cb);
          if (i >= 0) flushCbs.splice(i, 1);
        };
      },
      // Hard lock from the host (review cell, diff view). Renderer-owned soft
      // modes (e.g. a review flag in a sidecar file) sit UNDER this one.
      isReadOnly: () => !!readOnlyRef.current,
      canInteract:()=>interactionRef.current,
      // Annotation requests (lib/rendererAnnotation), the registry's
      // responder below: one handler per view, a second replaces the first.
      onAnnotationRequest: (handler: AnnotationHandler) => {
        if (!alive || typeof handler !== 'function') return () => {};
        annotationHandler = handler;
        notifyAnnotationResponders();
        return () => {
          if (annotationHandler !== handler) return;
          annotationHandler = null;
          for (const job of annotationJobs) job.abort();
          annotationJobs.clear();
          notifyAnnotationResponders();
        };
      },
      // Style injection into the shadow root (CSS isolation)
      injectCss: (css: string) => {
        const el = document.createElement('style');
        el.textContent = css;
        shadow.appendChild(el);
      },
      // Returns a Promise that resolves when the stylesheet has actually
      // loaded. Mirrors the iframe-side contract in sandbox.html so a
      // renderer can await all of its CSS before declaring ready().
      loadCss: (href: string): Promise<void> => {
        // A module-CDN stylesheet loads from the mirror: the import map
        // covers imports only. Compared by the mapped href.
        const mapped = mirrorUrl(href, getApiUrl());
        // Attribute compare, not a `link[href="…"]` selector: an href with a
        // quote or backslash would make querySelector throw.
        const existing: HTMLLinkElement | null =
          Array.from(shadow.querySelectorAll('link')).find(l => l.getAttribute('href') === mapped) ?? null;
        if (existing) {
          if (existing.sheet) return Promise.resolve();
          return new Promise<void>(resolve => {
            existing.addEventListener('load', () => resolve(), { once: true });
            existing.addEventListener('error', () => resolve(), { once: true });
          });
        }
        const l = document.createElement('link');
        l.rel = 'stylesheet';
        l.href = mapped;
        return new Promise<void>(resolve => {
          l.addEventListener('load', () => resolve(), { once: true });
          l.addEventListener('error', () => resolve(), { once: true });
          shadow.appendChild(l);
        });
      },
      // Renderer signals "I'm painted — drop the overlay." Guarded by the
      // run's liveness so a late ready() from a superseded mount can't
      // lift the overlay on top of a newer one. Clears the safety-net
      // timer since we don't need the fallback once the renderer has
      // explicitly reported readiness.
      ready: () => {
        if (!alive) return;
        if (readyFallbackTimerRef.current) {
          clearTimeout(readyFallbackTimerRef.current);
          readyFallbackTimerRef.current = null;
        }
        setIsReady(true);
      },
      // Tagged actions (agent-v2 §4.3.4): the emitting renderer's file is
      // stamped as `source` so generator-side waiters can filter by surface,
      // and an optional structured `payload` lets a card carry an answer
      // value, not just a click. Old renderers calling with one arg are
      // unchanged.
      emitAction: async (actionId: string, payload?: Record<string, unknown>): Promise<boolean> => {
        if (!cellId || !channel || !interactionRef.current) return false;
        const apiBase = getApiUrl();
        try {
          const res = await fetch(
            `${apiBase}/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/actions`,
            {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                action_id: actionId,
                source: fileName || '',
                ...(payload && typeof payload === 'object' ? { payload } : {}),
              }),
            },
          );
          return res.ok;
        } catch {
          return false;
        }
      },
// File operations use pinned bodies and conditional durable receipts.
      stageDraftFile: async (
        path: string,
        bytes: ArrayBuffer | Uint8Array | Blob | string,
      ): Promise<boolean> => {
        if (!cellId || !channel || !interactionRef.current) return false;
        try {
          let payload: ArrayBuffer | Uint8Array;
          if (typeof bytes === 'string') {
            payload = new TextEncoder().encode(bytes);
          } else if (bytes instanceof Blob) {
            payload = await bytes.arrayBuffer();
          } else {
            payload = bytes;
          }
          const { blob_hash } = await uploadBlob(payload);
          await writeCellBlob(channel, cellId, rendererRealPath(fileName,displayName,path), blob_hash);
          return true;
        } catch {
          return false;
        }
      },
      // Text sidecars share the durable document owner and conditional
      // service used by iframe SIBLING_WRITE. Server policy checks declared
      // run interactions. Resolves false on failure.
      saveSiblingFile: async (path: string, content: string): Promise<boolean> => {
        const { ok } = await saveSiblingFileInCell(cellId, rendererRealPath(fileName,displayName,path), content, !interactionRef.current);
        return ok;
      },
      // Reveal a file that lives in the renderer's OWN cell — shared
      // implementation with the iframe hosts' REVEAL_FILE handler (see
      // lib/revealFile.ts for the full contract).
      // Only `newTab` passes: taking the main focus area is the host's.
      revealFile: (path: string, options?: { newTab?: boolean }): void => revealCellFile(cellId, path, { newTab: options?.newTab === true }),
      // Theme signal — native renderers share the host's document so
      // CSS variables are already live via :root. These methods exist
      // for parity with the iframe api surface: code shared between
      // native + iframe renderers can call api.getTheme() without
      // branching on embed mode. Reads directly off documentElement
      // so the snapshot is always authoritative.
      getTheme: () => readThemeSnapshot(),
      onThemeChange: (cb: (t: ThemeSnapshot) => void) => {
        const handler = () => { try { cb(readThemeSnapshot()); } catch (err) { console.error(err); } };
        window.addEventListener('collimate:theme-changed', handler);
        // Fire immediately so subscribers get a value without a
        // second round trip (matches the iframe contract).
        try { cb(readThemeSnapshot()); } catch (err) { console.error(err); }
        return () => window.removeEventListener('collimate:theme-changed', handler);
      },
      // Editing preferences (spellcheck). The shadow root's editables
      // already inherit <html spellcheck>; these are for editors that set
      // their own attribute or run in a frame of their own. Read off
      // documentElement, where useEditingStore applies them, and fired
      // immediately on subscribe like onThemeChange.
      getEditing: () => readEditingSnapshot(),
      onEditingChange: (cb: (e: EditingSnapshot) => void) => {
        const handler = () => { try { cb(readEditingSnapshot()); } catch (err) { console.error(err); } };
        window.addEventListener(EDITING_CHANGED_EVENT, handler);
        try { cb(readEditingSnapshot()); } catch (err) { console.error(err); }
        return () => window.removeEventListener(EDITING_CHANGED_EVENT, handler);
      },
      // This renderer's style choices (theme, font, text size): its record
      // in the channel's renderer_styles section, all null for its
      // defaults. onStyleChange fires on subscribe and on every change to
      // THIS renderer id's record; setStyle persists a patch under this
      // renderer's id only. getFontFamilies is the font catalogue the style
      // control offers.
      getStyle: () => readRendererStyle(rendererId),
      onStyleChange: (cb: (s: RendererStyleRecord) => void) => {
        const handler = (e: Event) => {
          if ((e as CustomEvent<RendererStylesChangedDetail>).detail?.rendererId !== rendererId) return;
          try { cb(readRendererStyle(rendererId)); } catch (err) { console.error(err); }
        };
        window.addEventListener(RENDERER_STYLES_CHANGED_EVENT, handler);
        try { cb(readRendererStyle(rendererId)); } catch (err) { console.error(err); }
        return () => window.removeEventListener(RENDERER_STYLES_CHANGED_EVENT, handler);
      },
      setStyle: (patch: Partial<RendererStyleRecord>) => {
        if (!patch || typeof patch !== 'object') return;
        const clean: Partial<RendererStyleRecord> = {};
        if ('theme' in patch) clean.theme = patch.theme;
        if ('font' in patch) clean.font = patch.font;
        if ('textSize' in patch) clean.textSize = patch.textSize;
        void useRendererStylesStore.getState().commitStyle(rendererId, clean)
          .catch((err) => console.warn('[renderer style] save failed', err));
      },
      getFontFamilies: () => rendererFontFamilies(),
      // Visibility signal — true when the cell is on screen, false when the
      // host has scrolled it offscreen and display:none'd its subtree
      // (which clears GPU/canvas drawing buffers). Mirrors onThemeChange:
      // fires immediately on subscribe (so a renderer mounted while hidden
      // reads the right state) and on every flip thereafter. GPU/canvas
      // renderers repaint their buffer on visible=true — see useVisibility
      // in the SDK and the .terminal renderer for the reference pattern.
      getVisible: () => visibleRef.current,
      onVisibleChange: (cb: (visible: boolean) => void) => {
        visibilityCbsRef.current.add(cb);
        try { cb(visibleRef.current); } catch (err) { console.error(err); }
        return () => { visibilityCbsRef.current.delete(cb); };
      },
      // Cell-focus signal — true only while this cell holds the canvas
      // focus. Native renderers share the main document, so any global
      // input listener (window keydown etc.) MUST gate on this: an
      // unfocused cell must neither react to nor preventDefault input
      // that belongs to the rest of the app. Fires immediately on
      // subscribe, like onVisibleChange.
      getFocused: () => focusRef.current,
      onFocusChange: (cb: (focused: boolean) => void) => {
        focusCbsRef.current.add(cb);
        try { cb(focusRef.current); } catch (err) { console.error(err); }
        return () => { focusCbsRef.current.delete(cb); };
      },
      desktop: desktopSurface,
    };

    // Listen for FILES_CHANGED events from the parent. Scoped to this
    // renderer's cell — without the filter every native renderer refetched
    // its files on every commit anywhere in the channel, which multiplied a
    // commit burst into a channel-wide request storm.
    const handleFilesChanged = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.cellId !== cellId) return;
      for (const cb of filesChangedCbs) {
        try { cb({...detail,changedFiles:Array.isArray(detail.changedFiles)?rendererChangedFiles(fileName,displayName,detail.changedFiles):detail.changedFiles}); } catch (err) { console.error('native renderer files-changed error:', err); }
      }
    };
    window.addEventListener('collimate:files-changed-broadcast', handleFilesChanged);
    const unsubscribeSurface = subscribeSurface(fileChannel,fileCell,fileName,()=>{
      if(versionId || !alive)return;
      for(const callback of surfaceChangedCbs){
        try{callback();}catch(error){console.error('surface-changed callback failed',error);}
      }
    });

    const unregisterFlush = registerRendererFlusher(cellId,fileName,async()=>{
      await Promise.all(flushCbs.map(cb=>Promise.resolve().then(cb)));
    });
    const unregisterAnnotation = cellId ? registerAnnotationResponder({
      cellId, path: fileName, rendererId, viewKey: `native:${setContentOwner.viewId}`,
      visible: () => alive && visibleRef.current,
      focused: () => alive && focusRef.current,
      capable: () => alive && annotationHandler !== null,
      canInteract: () => interactionRef.current,
      selection: () => (alive && shadowRef.current ? selectedTextIn(shadowRef.current) : ''),
      request: (request: AnnotationRequest, signal: AbortSignal) => {
        const handler = annotationHandler;
        if (!handler) return Promise.reject(new AnnotationUnavailableError('not-capable', 'The renderer stopped taking annotations.'));
        const job = new AbortController();
        const stop = () => job.abort();
        signal.addEventListener('abort', stop, { once: true });
        annotationJobs.add(job);
        return Promise.resolve().then(() => handler({ ...request }, job.signal)).finally(() => {
          annotationJobs.delete(job);
          signal.removeEventListener('abort', stop);
        });
      },
    }) : () => {};

    // Dynamic import and mount — results are cached at module scope by
    // (rendererId, rendererHash). Concurrent mounts of the same renderer
    // coalesce on the shared Promise; sequential remounts (isOffscreen
    // toggle, fan-out toggle) skip the fetch/transpile entirely.
    const apiBase = getApiUrl().replace(/\/api\/?$/, '');

    Promise.all([
      getCachedRenderer(rendererId, rendererHash, apiBase),
      fileChannel && fileCell ? cellFileCap(fileChannel, fileCell, versionId) : null,
    ])
      .then(async ([{ module: moduleObj, moduleUrl }, cap]) => {
        // Bail if a newer effect run has superseded us. Without this guard a
        // late-resolving fetch can mount onto a host that the next run already
        // cleared, producing flicker or wrong content during rapid switches.
        if (!alive) return;
        if (cap) fileBase = cellFileBase(fileChannel, fileCell, cap, versionId);

        // Input containment: this mounted instance's focus feeds the gate on
        // any input listener the module registered (see
        // lib/rendererInputContainment.ts). Unregistered in the cleanup.
        containmentUnregRef.current?.();
        containmentUnregRef.current =
          registerRendererInstance(moduleUrl, () => focusRef.current);

        // Unwrap default export for new SDK renderers
        const renderer = moduleObj.default || moduleObj;

        // A run that died during the (possibly async) mount must not store
        // its cleanup: the effect cleanup already ran, so call it now.
        const adoptCleanup = (cleanup: unknown): boolean => {
          if (!alive) {
            if (typeof cleanup === 'function') { try { cleanup(); } catch { /* safety */ } }
            return false;
          }
          if (typeof cleanup === 'function') cleanupRef.current = cleanup as () => void;
          return true;
        };

        if (isDiffMode && typeof renderer.mountDiff === 'function') {
          if (!adoptCleanup(await renderer.mountDiff(mountEl, api))) return;
        } else if (typeof renderer.mount === 'function') {
          if (!adoptCleanup(await renderer.mount(mountEl, api))) return;
        } else {
          setMountError(`Renderer '${rendererId}' does not export mount()`);
          setIsReady(true);
          return;
        }
        // Safety-net fallback: if the renderer never calls api.ready() (e.g.
        // an older renderer that predates the protocol, or one that crashes
        // silently), drop the overlay after 3s so the user isn't stuck behind
        // a spinner forever. Well-behaved renderers call api.ready() earlier
        // and this timer becomes a no-op.
        readyFallbackTimerRef.current = setTimeout(() => {
          if (alive) setIsReady(true);
        }, 3000);
      })
      .catch((err) => {
        console.error('NativeRenderer mount failed:', err);
        // Only the mount that still owns the host may paint the failure —
        // a superseded run's late rejection must not cover a newer mount.
        if (!alive) return;
        setMountError(String(err?.message ?? err));
        // Even on failure we drop the overlay so the error panel is visible.
        setIsReady(true);
      });

    return () => {
      // Explicit operations refuse before DOM removal. Forced native disposal
      // keeps renderer resources alive through its bounded serialization, and
      // persists through the captured owner after the parent's listener is gone.
      const needsFlush=latestIdentity.current===mountedIdentity;
      const ownedContent=fileContentRef.current;
      const persist=disposalSave.current;
      const rendererCleanup=cleanupRef.current;
      cleanupRef.current=null;
      if(needsFlush){
        disposing=true;
        persistDisposal=(content)=>{
          if(latestIdentity.current!==mountedIdentity || fileContentRef.current!==ownedContent || readOnlyRef.current)return;
          disposalWrites.push(Promise.resolve(persist?.(content)));
        };
      }
      alive = false;
      if(mountedDocumentOwner)mountedDocumentOwner.forgetView(setContentOwner.viewId);
      else if(cellId)forgetCellDocumentView(fileChannel,fileCell,fileName,setContentOwner.viewId);
      window.removeEventListener('collimate:files-changed-broadcast', handleFilesChanged);
      unsubscribeSurface();surfaceChangedCbs.clear();
      unregisterFlush();
      unregisterAnnotation();
      annotationHandler = null;
      for (const job of annotationJobs) job.abort();
      annotationJobs.clear();
      // Blob URL is owned by the session-wide renderer cache — do not
      // revoke here, or a re-mount with the same key gets a dead URL.
      if (readyFallbackTimerRef.current) {
        clearTimeout(readyFallbackTimerRef.current);
        readyFallbackTimerRef.current = null;
      }
      containmentUnregRef.current?.();
      containmentUnregRef.current = null;
      const finish=()=>{
        disposing=false;
        if(rendererCleanup){try{rendererCleanup();}catch(error){console.error('renderer cleanup failed',error);}}
        for(const handle of mountedGuests){try{handle.destroy();}catch(error){console.error('guest destroy failed',error);}}
        mountedGuests.length=0;
        if(mountedGuestsRef.current===mountedGuests)mountedGuestsRef.current=[];
      };
      if(needsFlush && flushCbs.length){
        void boundedFlush(async()=>{
          await Promise.all(flushCbs.map(cb=>cb()));
          await Promise.all(disposalWrites);
        }).then(finish,error=>{console.error('Renderer disposal save failed',error);finish();});
      }else finish();
      // Layout delivery already checks ownership; cleanup then retires this
      // exact callback set without clearing a later mount's owner.
      setContentCbs.length = 0;
      documentSnapshotCbs.clear();
      if (setContentOwnerRef.current === setContentOwner) setContentOwnerRef.current = null;
      // These Sets live as long as the component, so empty them per mount
      // like setContentCbs: a dead mount's subscribers must not hear the next one's flips.
      visibilityCbs.clear();
      focusCbs.clear();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rendererId, rendererHash, fileName, displayName, cellId, versionId, channel, isDiffMode, diffContent,documentOwnerId, handleContentChange, attempt]);

  return (
    <div data-native-renderer className="w-full h-full relative">
      <div
        ref={hostRef}
        className="w-full h-full"
        style={{
          // Passive while an OS file drag is over the window too: a
          // renderer's own inline <iframe> in the shadow root would swallow
          // the drag the Files pane and the canvas beneath are waiting for.
          pointerEvents: isActiveFocus && !osFileDragActive ? 'auto' : 'none',
          // Conceal hides the shadow-DOM content itself, mirroring what
          // SandboxIframe does for pooled iframes: during a tab drag the
          // pane drops to its background so the split zones read — and,
          // critically, a renderer's own inline <iframe> in the shadow root
          // leaves hit-testing, so dragover reaches the leaf body instead
          // of dying inside the iframe's own document.
          visibility: concealed ? 'hidden' : undefined,
          opacity: isReady ? 1 : 0,
          // Only animate the reveal (isReady flipping true). When isReady
          // resets to false on a new mount, snap to 0 so the overlay can
          // cover cleanly — interpolating 1→0 over 380ms here let the
          // white underlay show through both half-transparent layers
          // during the gap, producing the "flash white" regression.
          transition: isReady ? 'opacity 380ms ease-out' : 'none',
        }}
      />
      {/* Always-mounted overlay so the spinner fades out smoothly instead of
          unmounting in a single frame (the prior `{!isReady && …}` path was
          the source of the visible "pop" on first paint). */}
      <div
        className="absolute inset-0 z-20 bg-sheet flex items-center justify-center pointer-events-none"
        style={{
          opacity: isReady ? 0 : 1,
          // Snap to opaque when isReady resets to false (the renderer
          // is about to remount); only fade out when the renderer
          // signals ready. Symmetrical with the host above.
          transition: isReady ? 'opacity 380ms ease-out' : 'none',
        }}
        onTransitionEnd={(e) => { if (e.target === e.currentTarget && isReady) setSpinnerParked(true); }}
      >
        {!spinnerParked && (
          <div className="flex flex-col items-center gap-2">
            <div className="w-4 h-4 rounded-full border-2 border-teal/30 border-t-teal animate-spin" />
            <span className="animate-pulse text-[0.75rem] font-mono text-ink-3 select-none">Loading renderer…</span>
          </div>
        )}
      </div>
      {/* Failure floor: the renderer never mounted (fetch/transpile/mount
          threw, or the module has no mount()). Plain-language line first,
          raw detail in mono, then Retry (full reload — the cache evicted the
          failed entry) or a zero-dependency plain-text view of the file. */}
      {mountError !== null && (
        showPlainText ? (
          <div className="absolute inset-0 z-30 bg-sheet flex flex-col">
            <div className="flex items-center gap-2 h-[1.875rem] px-3 border-b border-line text-[0.71875rem] text-ink-3 shrink-0">
              <span className="truncate">Renderer failed — showing plain text</span>
              <button
                type="button"
                onClick={() => { setShowPlainText(false); setMountError(null); setIsReady(false); setAttempt(a => a + 1); }}
                className={cn(btn, 'ml-auto h-[1.375rem] px-2')}
              >
                Retry
              </button>
            </div>
            <pre className="flex-1 min-h-0 overflow-auto m-0 p-3 bg-code-bg text-[0.75rem] font-mono text-ink whitespace-pre-wrap break-words select-text">{fileContent}</pre>
          </div>
        ) : (
          <div className="absolute inset-0 z-30 bg-sheet flex items-center justify-center p-4 overflow-auto">
            <div className="max-w-full flex flex-col items-center gap-3 text-center">
              <div className="text-[0.8125rem] text-ink">This file&apos;s renderer failed to load.</div>
              <div className="text-[0.71875rem] font-mono text-red whitespace-pre-wrap break-words max-w-full select-text">{mountError}</div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => { setMountError(null); setShowPlainText(false); setIsReady(false); setAttempt(a => a + 1); }}
                  className={btnPrimary}
                >
                  Retry
                </button>
                <button
                  type="button"
                  onClick={() => setShowPlainText(true)}
                  className={btn}
                >
                  Open as plain text
                </button>
              </div>
            </div>
          </div>
        )
      )}
      {!isActiveFocus && (
        <div
          className="absolute inset-0 z-10 cursor-pointer"
          title="Click to focus"
          // mouseUp, not click: a DRAG that starts on the scrim (e.g. trying
          // to select message text in an unfocused cell) never produces a
          // click, so it used to die silently — the scrim stayed and nothing
          // focused. mouseup fires for clicks and drags alike, at the point
          // the pointer released, so both gestures focus the cell (and the
          // synthetic re-dispatch makes a plain click act, same as before).
          onMouseUp={handleScrimClick}
          // The press itself selects; the editable under it takes the
          // keyboard once the host takes pointer events (lib/selectionFocus).
          onPointerDown={handleScrimPress}
          ref={scrimWheelRef}
        />
      )}
    </div>
  );
};
