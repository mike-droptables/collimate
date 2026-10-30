import type {DocumentViewEdit,DocumentViewSnapshot} from '@/lib/fileDocument';
import {registerSnapshotCodec,type RendererDocumentSnapshot,type RendererRecoverySnapshot} from '@/lib/fileSnapshotCodec';
import {isRunPath} from '@/lib/runningCell';
import {useRendererFilename} from '@/lib/rendererAliases';
import {currentChannelId} from '@/lib/currentChannel';
import {handleSandboxHostMessage, disposeSandboxView, pushSandboxTheme, pushSandboxEditing, pushSandboxStyle, pushSandboxKeys} from '@/lib/sandboxBridge';
import {EDITING_CHANGED_EVENT} from '@/lib/editingSnapshot';
import {RENDERER_STYLES_CHANGED_EVENT, type RendererStylesChangedDetail} from '@/lib/rendererStyles';
import {registerRendererFlusher, flushSandbox} from '@/lib/rendererFlush';
import {registerAnnotationResponder, notifyAnnotationResponders, requestSandboxAnnotation, selectedTextInFrame, AnnotationUnavailableError} from '@/lib/rendererAnnotation';
import React, { useCallback, useEffect, useMemo, useRef } from 'react';
import { useIsPresent } from 'motion/react';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { useMoving } from '@/contexts/MovingContext';
import { usePooledIframeConcealed, useRendererConcealed } from '@/contexts/RendererConcealContext';
import { onForwardedCombosChange } from '@/lib/keybindings/forwardedCombos';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { buildSandboxSrc } from '@/lib/sandboxSrc';
import { createWheelReplay, scrollTargetInFrame } from '@/lib/wheelReplay';
import { afterScrimPress, focusFrameAt } from '@/lib/selectionFocus';

interface SandboxIframeProps {
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
   *  `readonly=1` so the renderer's api.isReadOnly() tells the truth instead
   *  of the host silently discarding the saves it invites. */
  readOnly?: boolean;
  /** The pool evicted this blob-keyed document while it was parked, and
   *  the frame would be rebuilt from the key's blob (the bytes the editor
   *  opened with). Return true when the host replaces the document from
   *  its live text under a new key: this acquire is then skipped. False
   *  (or no handler) re-creates the frame from the current src. */
  onDocumentRecreated?: () => boolean;
}

export const SandboxIframe: React.FC<SandboxIframeProps> = ({
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

  const containerRef = useRef<HTMLDivElement>(null);
  const [isVisible, setIsVisible] = React.useState(false);
  // The annotation responder's probes (section 4): this view's visibility
  // and focus, and whether the frame's renderer subscribed a handler
  // (ANNOTATION_CAPABLE, re-posted by the frame on every PING_READY).
  const visibleRef = useRef(false);
  const focusRef = useRef(isActiveFocus);
  const capableRef = useRef(false);
  const [isReady, setIsReady] = React.useState(false);
  // True once the overlay's fade-out has finished, so its infinite spinner
  // animations stop ticking under an invisible parent. Reset on re-cover.
  const [spinnerParked, setSpinnerParked] = React.useState(false);
  if (!isReady && spinnerParked) setSpinnerParked(false);
  const { acquire, release, setPointerEvents, getContentWindow, getElement, waitUntilReady, setVisible, onMessage, registerMotionLayer } = useGuestPool();
  // The host box rides its card and the camera: its own compositor layer
  // only while the scene moves (lib/motionLayers).
  useEffect(() => {
    const container = containerRef.current;
    return container ? registerMotionLayer(container) : undefined;
  }, [registerMotionLayer]);
  const { isMoving } = useMoving();
  const osFileDragActive = useCollimateStore(s => s.osFileDragActive);
  // The host cell is animating out (remove burn, dissolve) or drag-hidden.
  // Hide the pooled iframe for the duration — without releasing it, so a
  // reversible state (drag dropped back in place) restores instantly.
  // The full conceal (cell animations, overlays) OR the iframe-only one a
  // tab drag raises (ContentPane) — either hides the pooled iframe.
  const fullConceal = useRendererConcealed();
  const iframeConceal = usePooledIframeConcealed();
  const concealed = fullConceal || iframeConceal;
  // Read by the acquire effect: the pool reveals an already-ready entry on
  // un-park, which must not override a conceal that is already on.
  const concealedRef = useRef(concealed);
  useEffect(() => { concealedRef.current = concealed; }, [concealed]);
  const rendererIdRef = useRef(rendererId);
  useEffect(() => { rendererIdRef.current = rendererId; }, [rendererId]);
  // Hooks into the nearest AnimatePresence ancestor. While a cell / stack is
  // exit-animating, its container stays mounted (so the fade can play) but the
  // pooled iframe lives in a sibling world-space overlay and would otherwise
  // stay at full opacity on top — the renderer "sticks around" during the
  // fade. Flipping isVisible false on exit releases the iframe via the
  // existing visibility effect, letting the cell animate out cleanly.
  //
  // MUST be the read-only useIsPresent, NOT usePresence: usePresence
  // registers this component as a removal blocker, and nothing here ever
  // called its safeToRemove — so every AnimatePresence exit containing a
  // pooled-iframe renderer (e.g. a stack fading out when it fans out)
  // finished its fade but stayed mounted forever at opacity 0,
  // hit-testable, silently swallowing clicks aimed at whatever rendered
  // underneath its footprint (the fan columns' tab strips, file rows, …).
  const isPresent = useIsPresent();
  // Mirror isPresent into a ref so the IntersectionObserver callback (which
  // captures the closure once at mount) can read the live value. Without this
  // an observer fire while the component is exit-animating would flip
  // isVisible back to true and un-park an iframe that the !isPresent effect
  // below (setIsVisible(false)) is actively releasing — the "renderers stick
  // around after a fan-out toggle" symptom.
  const isPresentRef = useRef(isPresent);
  useEffect(() => { isPresentRef.current = isPresent; }, [isPresent]);

  const poolId = documentId ?? JSON.stringify([channel || currentChannelId(),cellId,fileName,versionId??'current',rendererId,rendererHash,diffHash?'diff':'view']);

  useEffect(() => {
    if (!isPresent) setIsVisible(false);
  }, [isPresent]);

  // Gate the pooled iframe's visibility on RENDERER_READY (reflected in
  // isReady). The sandbox.html `load` event fires well before any
  // CSS-heavy renderer (monaco, crepe, excalidraw) has actually painted —
  // revealing on load showed an un-painted iframe over the spinner, which
  // is the "white flash / no spinner" regression. When the src swaps
  // (poolId change) or a drag reparents, the effect re-fires with
  // isReady=false, hiding the iframe again until the next paint.
  // `isVisible` is a dep so that re-acquiring after a park (viewport
  // scroll-away-and-back, every document) reapplies visibility —
  // parkOffscreen had set it hidden, and without this the iframe would stay
  // invisible since neither poolId nor isReady changed across the cycle.
  useEffect(() => {
    if (isVisible) setVisible(poolId, isReady && !concealed, containerRef.current ?? undefined);
  }, [isVisible, poolId, isReady, concealed, setVisible]);

  // channel rides the sandbox URL so api.getFileUrl can build direct
  // cell-file URLs synchronously inside the iframe (<img src> etc.).
  const srcChannel = channel || currentChannelId();
  const sandboxSrc = useMemo(
    () => buildSandboxSrc({ rendererId, rendererHash, fileHash: documentId ? '' : fileHash, documentId, diffHash, diffLabels, fileName, displayName, cellId, versionId, channel: srcChannel, readOnly:documentId?false:readOnly }),
    [rendererId, rendererHash, documentId ? '' : fileHash, documentId, diffHash, diffLabels, fileName, displayName, cellId, versionId, srcChannel, readOnly],
  );

  // Reset ready state when the pool-id changes — a new file/renderer/hash
  // means the iframe's src is swapping and we need to fade in again — or
  // when the src changes under the same poolId (e.g. a readOnly/diffLabels
  // flip), which reloads the element in place.
  useEffect(() => { setIsReady(false); }, [poolId, sandboxSrc]);
  // A new frame has said nothing yet about annotation requests.
  useEffect(() => { capableRef.current = false; notifyAnnotationResponders(); }, [poolId, sandboxSrc]);
  useEffect(() => {
    visibleRef.current = isVisible;
    focusRef.current = isActiveFocus;
    notifyAnnotationResponders();
  }, [isVisible, isActiveFocus]);

  // ----- 1. Viewport Culling (Intersection Observer) -----
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new IntersectionObserver(([entry]) => {
      // Don't flip isVisible back to true while the component is mid-exit
      // animation. During a fan-out toggle the exiting real StackGroup
      // (under AnimatePresence) is still in the DOM (and still intersecting)
      // until its motion exit completes, but the !isPresent effect has already
      // set isVisible false to release the iframe. Letting the observer
      // override that would un-park the iframe over its old rect — the
      // "renderers stick around after a fan-out toggle" symptom. False is
      // always allowed.
      if (!isPresentRef.current && entry.isIntersecting) return;
      setIsVisible(entry.isIntersecting);
    }, { threshold: 0.1 });
    observer.observe(container);

    // Force a fresh observation on fan-out toggles and other scene-level
    // layout swaps. The observer's initial fire for newly-mounted cells
    // can land before motion.div has applied its transform — and once
    // it's recorded "not intersecting" the next layout shift may not
    // cross the threshold strongly enough to refire. unobserve+observe
    // posts a new initial entry against the current rect.
    const reobserve = () => {
      observer.unobserve(container);
      observer.observe(container);
    };
    window.addEventListener('collimate:scene-invalidated', reobserve);

    return () => {
      observer.disconnect();
      window.removeEventListener('collimate:scene-invalidated', reobserve);
    };
  // Observer visibility is component-local; document release remains host-owned.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Parking and eviction release a view only. The document owner retains bytes.
  useEffect(() => {
    const container=containerRef.current;
    if(!container)return;
    if(!isVisible){release(poolId,container);return;}
    acquire(poolId,sandboxSrc,container,cellId,disposeSandboxView);
    if(concealedRef.current)setVisible(poolId,false,container);
      // Two-stage readiness: the iframe LOAD event only means the HTML shell
      // is mounted — crepe/milkdown and other CSS-heavy renderers are still
      // fetching stylesheets and running setup at that point. We wait for an
      // explicit RENDERER_READY postMessage (fired by api.ready() inside the
      // sandbox). If a renderer never signals, a 3s timeout drops the overlay
      // as a safety net so broken renderers don't stall forever.
      //
      // Pool re-use: when the pool hands us an iframe that's already fully
      // loaded & rendered, the original RENDERER_READY went to the previous
      // consumer. We PING the iframe after acquire; if it's already ready it
      // echoes RENDERER_READY back immediately so the overlay drops without
      // the user sitting through the safety-net timeout. A frame parked
      // mid-boot also re-sends the preview requests no host answered while
      // it was parked (sandbox.html PING_READY), which this host now owns.
      let cancelled = false;
      let fallbackTimer: ReturnType<typeof setTimeout> | null = null;
      waitUntilReady(poolId).then(() => {
        if (cancelled) return;
        const target = getContentWindow(poolId);
        // The theme, the editing preferences and the renderer's style
        // choices before the renderer paints, so its editors never flash
        // the wrong spellcheck, theme or text size.
        if (target) {
          pushSandboxTheme(target);
          pushSandboxEditing(target);
          pushSandboxStyle(target, rendererIdRef.current);
        }
        target?.postMessage({ type: 'PING_READY' }, window.location.origin);
        fallbackTimer = setTimeout(() => {
          if (!cancelled) setIsReady(true);
        }, 3000);
      });

      return () => {
        cancelled = true;
        if (fallbackTimer) clearTimeout(fallbackTimer);
        release(poolId,container);
      };

  }, [isVisible,poolId,sandboxSrc,acquire,release,setVisible,getContentWindow,waitUntilReady,cellId]);

  useEffect(()=>{
    if(isReady && fileContent!==undefined)getContentWindow(poolId)?.postMessage({type:'SET_DOCUMENT',...documentSnapshot,content:fileContent,readOnly:!!readOnly||!!versionId||!!diffHash||isRunPath(fileName),canInteract:!readOnly&&!versionId&&!diffHash},window.location.origin);
  },[isReady,fileContent,readOnly,versionId,diffHash,fileName,documentSnapshot,poolId,getContentWindow]);

  // ----- 3. Pointer Events -----
  // Disable during canvas pan or when not focused (focus-blocker div handles
  // clicks instead), and while an OS file drag is over the window: the
  // iframe's own document would swallow the drag the Files pane and the
  // canvas beneath it are waiting for.
  // sandboxSrc is a dep: a same-poolId src change re-parks the element via release().
  useEffect(() => {
    setPointerEvents(poolId, isVisible && isActiveFocus && !isMoving && !concealed && !osFileDragActive);
  }, [isVisible, isActiveFocus, isMoving, concealed, osFileDragActive, poolId, sandboxSrc, setPointerEvents]);

  // ----- 3a. First-wheel replay -----
  // A vertical wheel over the focus blocker focuses the cell (StackItem,
  // ContentPane) and is replayed at once into the scroller under the
  // pointer in the pooled iframe's own document (same-origin pool), which
  // hit-tests whatever the element's pointer-events. Later wheels land
  // natively once the iframe takes pointer events. Propagation is kept:
  // the bubble selects.
  const handleBlockerWheel = useMemo(() => createWheelReplay(
    (x, y, dx, dy) => {
      const frame = getElement(poolId);
      return frame?.tagName === 'IFRAME' ? scrollTargetInFrame(frame as HTMLIFrameElement, x, y, dx, dy) : null;
    },
    (run) => run(),
  ), [getElement, poolId]);

  // ----- 3a'. First-press focus -----
  // The focus blocker takes the press that selects the cell, so the frame
  // never sees it and the keys went nowhere. Once the frame takes pointer
  // events, the editable under the press takes the keyboard, unless
  // something else took it meanwhile (lib/selectionFocus).
  const handleBlockerPointerDown = useCallback((e: React.PointerEvent) => {
    if (e.button !== 0) return;
    afterScrimPress(e.clientX, e.clientY, (x, y) => {
      const frame = getElement(poolId);
      if (frame?.tagName === 'IFRAME') focusFrameAt(frame as HTMLIFrameElement, x, y);
    });
  }, [getElement, poolId]);

  // ----- 3b. Keybinding forwarding — push the current combo list into the
  // iframe on ready and whenever the user remaps a renderer-allowed
  // shortcut or binds a command. The iframe forwards matching keydowns back via
  // FORWARD_KEYDOWN (handled below).
  useEffect(() => {
    if (!isReady) return;
    const post = () => {
      const cw = getContentWindow(poolId);
      if (!cw) return;
      pushSandboxKeys(cw);
    };
    post();
    return onForwardedCombosChange(post);
  }, [isReady, poolId, getContentWindow]);

  // ----- 3c. Theme forwarding — push the host's active theme on every
  // iframe ready and whenever ThemeProvider broadcasts a change. The
  // iframe mirrors vars onto :root and fires any api.onThemeChange(cb)
  // subscribers so Monaco/Crepe/… can swap their internal theme
  // without needing a remount.
  useEffect(() => {
    if (!isReady) return;
    const post = () => {
      const cw = getContentWindow(poolId);
      // Shared snapshot helper so this list stays in lockstep with
      // what NativeRenderer's api.getTheme() returns.
      if (cw) pushSandboxTheme(cw);
    };
    post();
    window.addEventListener('collimate:theme-changed', post);
    return () => window.removeEventListener('collimate:theme-changed', post);
  }, [isReady, poolId, getContentWindow]);

  // ----- 3d. Editing forwarding — push the editing preferences
  // (spellcheck) on every iframe ready and whenever useEditingStore applies
  // a change. The sandbox sets them on its own <html>, which the renderer's
  // editables inherit, and fires its api.onEditingChange subscribers.
  useEffect(() => {
    if (!isReady) return;
    const post = () => {
      const cw = getContentWindow(poolId);
      if (cw) pushSandboxEditing(cw);
    };
    post();
    window.addEventListener(EDITING_CHANGED_EVENT, post);
    return () => window.removeEventListener(EDITING_CHANGED_EVENT, post);
  }, [isReady, poolId, getContentWindow]);

  // ----- 3e. Style forwarding — push this renderer's style choices (its
  // record in the channel's renderer_styles section, and the font
  // catalogue) on every iframe ready and whenever useRendererStylesStore
  // changes THIS renderer id's record. The sandbox fires its
  // api.onStyleChange subscribers.
  useEffect(() => {
    if (!isReady) return;
    const post = () => {
      const cw = getContentWindow(poolId);
      if (cw) pushSandboxStyle(cw, rendererId);
    };
    const onChange = (e: Event) => {
      if ((e as CustomEvent<RendererStylesChangedDetail>).detail?.rendererId === rendererId) post();
    };
    post();
    window.addEventListener(RENDERER_STYLES_CHANGED_EVENT, onChange);
    return () => window.removeEventListener(RENDERER_STYLES_CHANGED_EVENT, onChange);
  }, [isReady, poolId, getContentWindow, rendererId]);

  // ----- 4. Shared postMessage bridge -----
  // Registered through the pool's shared dispatcher, which delivers only
  // messages whose e.source is OUR iframe's contentWindow (same-origin
  // checked there too) — no per-consumer window listener, no source
  // filtering here.
  useEffect(() => {
    let alive = true;
    const handleMessage = (e: MessageEvent) => {
      const target = getContentWindow(poolId);
      if (target) handleSandboxHostMessage(e, {target,isCurrent:()=>alive && getContentWindow(poolId)===target,
        ready:()=>{setIsReady(true);setVisible(poolId,!concealedRef.current,containerRef.current??undefined);},rendererId,rendererHash,fileName,channel,cellId,versionId,readOnly:readOnly||!!diffHash,getDocument:()=>({...snapshotRef.current,content:contentRef.current??'',diffContent:diffRef.current??null}),getContent:()=>contentGetter.current?contentGetter.current():Promise.resolve(contentRef.current??''),registerSnapshotCodec,onSnapshotChange:(snapshot,view)=>snapshotChange.current?.(snapshot,view),onContentChange:(body,view)=>changeRef.current?.(body,view),
        onAnnotationCapable:(capable)=>{capableRef.current=capable;notifyAnnotationResponders();}});
    };
    const unregisterFlush = registerRendererFlusher(cellId,fileName,()=>{
      const target=getContentWindow(poolId);
      return target ? flushSandbox(target,()=>alive && getContentWindow(poolId)===target) : undefined;
    });
    // This view as an annotation responder (lib/rendererAnnotation): the
    // request rides the sandbox wire to whichever frame the pool gives us.
    const unregisterAnnotation = cellId ? registerAnnotationResponder({
      cellId, path: fileName, rendererId, viewKey: `sandbox:${poolId}`,
      visible: () => alive && visibleRef.current,
      focused: () => alive && focusRef.current,
      capable: () => alive && capableRef.current,
      canInteract: () => !readOnly && !versionId && !diffHash,
      selection: () => (alive ? selectedTextInFrame(getContentWindow(poolId)) : ''),
      request: (request, signal) => {
        const target = getContentWindow(poolId);
        return target
          ? requestSandboxAnnotation(target, request, signal, () => alive && getContentWindow(poolId) === target)
          : Promise.reject(new AnnotationUnavailableError('parked', 'The file’s view is not loaded.'));
      },
    }) : () => {};
    const unsubscribe = onMessage(poolId, handleMessage);
    return () => { alive = false; unregisterFlush(); unsubscribe(); unregisterAnnotation(); };
  }, [fileName, channel, cellId, versionId, readOnly, diffHash, poolId, onMessage, getContentWindow, rendererId, rendererHash]);

  return (
    <div
      ref={containerRef}
      className="w-full h-full relative"
    >
      {!isVisible && (
        <div className="w-full h-full bg-sheet flex items-center justify-center text-ink-3 select-none">
          <span className="animate-pulse text-[0.75rem] font-mono">Sleeping (Scroll to Wake)</span>
        </div>
      )}

      {/* Fade-in overlay: covers the iframe until its first paint lands,
          masking the brief unstyled-content flash (notably crepe/milkdown
          stylesheets that resolve after the first paint). Always mounted
          while the iframe is visible so opacity transitions cover both the
          spinner-in (becoming not-ready on src swap) and spinner-out (ready). */}
      {isVisible && (
        <div
          className="absolute inset-0 z-20 bg-sheet flex items-center justify-center pointer-events-none"
          style={{
            opacity: isReady ? 0 : 1,
            transition: 'opacity 380ms ease-out',
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
      )}

      {/* Focus blocker: sits above the placeholder when not focused.
          The overlay iframe has pointer-events:none in this state, so clicks
          and wheels pass through it and land here. */}
      {!isActiveFocus && (
        <div
          className="absolute inset-0 z-10 cursor-pointer"
          title="Click to Focus and Edit"
          onWheel={handleBlockerWheel}
          onPointerDown={handleBlockerPointerDown}
        />
      )}
    </div>
  );
};
