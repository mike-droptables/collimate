/**
 * Renderer annotation requests: the host side of `api.onAnnotationRequest`.
 *
 * A user attaching part of a file to a message (the composer's Add
 * annotation) asks the file's mounted view for context. Every host that
 * mounts a renderer for a cell file registers one responder here
 * (NativeRenderer directly, SandboxIframe and StandaloneSandboxIframe over the
 * sandbox wire, `requestSandboxAnnotation`); `requestAnnotation` routes the
 * request to one of them and resolves an outcome, never a rejection.
 *
 * Routing: among the (cell, path) responders that are capable (the renderer
 * subscribed a handler) and may interact (no history, review, diff or
 * read-only lock), a pick takes the focused one, waiting up to
 * ANNOTATION_FOCUS_WAIT_MS for the focus it asked for; otherwise the focused,
 * else the visible, else the latest registered. A file with no mounted view
 * is opened in a new tab first (revealCellFile), and a pick also takes the
 * main focus area so the view's focus blocker does not eat the pick's click.
 * One request per cell: a second one cancels the first. The ceiling is
 * ANNOTATION_PICK_MS for both modes; Esc on the window cancels a pick (the
 * scene's keyboard handler yields while hasPendingAnnotationPick()). A pick
 * whose view loses its focus or its visibility is cancelled; a view that
 * goes away answers `unavailable` (`parked`).
 *
 * The iframe wire (sandbox.html): host→frame ANNOTATION_REQUEST {requestId,
 * mode, hint?} and ANNOTATION_CANCEL {requestId}; frame→host ANNOTATION_ACK
 * {requestId} (a handler took it), ANNOTATION_RESULT {requestId,
 * context|null, cancelled?, unsupported?, unavailable?, error?} and
 * ANNOTATION_CAPABLE {capable}, which the frame re-posts on PING_READY. A
 * request is never kept for re-sending: a missing ACK within ANNOTATION_ACK_MS
 * answers `not-capable`.
 *
 * A renderer that cannot answer rejects with an Error named
 * 'AnnotationUnavailable' (the SDK's `annotationUnavailable(message)`); the
 * outcome is then `unavailable` with reason `renderer` and its message.
 *
 * Every `unavailable` outcome also carries the file's view as it was when
 * the request was made (`AnnotationView`: the best view's renderer id and
 * the text it had selected, read before the request reveals or focuses
 * anything), or the view mounted by the time it failed, or null when none
 * is mounted. The composer attaches the whole file from it, with that
 * selection as the attachment's text, when the view cannot pick a part.
 */
import { useSyncExternalStore } from 'react';
import { revealCellFile } from './revealFile';
import { useCollimateStore } from '@/stores/useCollimateStore';

export type AnnotationMode = 'pick' | 'selection';
export interface AnnotationRequest { id: string; mode: AnnotationMode; hint?: string }
export interface AnnotationContext { label: string; text: string; anchor?: Record<string, unknown> }
export type AnnotationHandler = (request: AnnotationRequest, signal: AbortSignal) => Promise<AnnotationContext | null>;
export type AnnotationUnavailableReason = 'not-open' | 'not-capable' | 'read-only' | 'parked' | 'renderer';
/** A mounted view of the file: its renderer and what it has selected. */
export interface AnnotationView { rendererId: string; selection: string }
export type AnnotationOutcome =
  | { status: 'ok'; path: string; rendererId: string; context: AnnotationContext }
  | { status: 'cancelled' }
  | { status: 'unavailable'; reason: AnnotationUnavailableReason; message: string; view: AnnotationView | null }
  | { status: 'error'; message: string };

/** How long an iframe view has to acknowledge a request (it has a handler). */
export const ANNOTATION_ACK_MS = 2000;
/** The ceiling on one request, picks and selections alike. */
export const ANNOTATION_PICK_MS = 300_000;
/** How long a file opened for a request has to mount a capable view. */
export const ANNOTATION_OPEN_WAIT_MS = 5000;
/** How long a pick waits for the focus it asked for before taking any view. */
export const ANNOTATION_FOCUS_WAIT_MS = 1000;
/** Bounds the host enforces on an answer, in characters. */
export const ANNOTATION_LABEL_MAX = 120;
export const ANNOTATION_TEXT_MAX = 16_384;
export const ANNOTATION_ANCHOR_MAX = 4096;
/** The Error name of a renderer's "this view cannot answer". */
export const ANNOTATION_UNAVAILABLE = 'AnnotationUnavailable';

/** A host's own refusal, carrying the outcome's reason. */
export class AnnotationUnavailableError extends Error {
  readonly reason: AnnotationUnavailableReason;
  constructor(reason: AnnotationUnavailableReason, message: string) {
    super(message);
    this.name = ANNOTATION_UNAVAILABLE;
    this.reason = reason;
  }
}

/** One mounted view of a cell file. The probes read the host's live state;
 *  the host calls notifyAnnotationResponders() whenever one may change. */
export interface AnnotationResponder {
  cellId: string; path: string; rendererId: string; viewKey: string;
  visible(): boolean; focused(): boolean; capable(): boolean; canInteract(): boolean;
  /** The text the view has selected right now ('' when none): its
   *  document's selection (`selectedTextIn`), whether or not it has the
   *  focus, so the selection survives the press that asks for it. */
  selection(): string;
  request(request: AnnotationRequest, signal: AbortSignal): Promise<AnnotationContext | null>;
}

/** The text selected inside `root` (a native renderer's shadow root, or a
 *  same-origin frame's document), clipped to ANNOTATION_TEXT_MAX; '' when
 *  the selection is collapsed or lies elsewhere. A shadow root's own
 *  selection (Chromium) is preferred to the document's retargeted one. */
export function selectedTextIn(root: Document | ShadowRoot): string {
  const own = (root as { getSelection?: () => Selection | null }).getSelection;
  const selection = own ? own.call(root) : root.ownerDocument?.getSelection() ?? null;
  if (!selection || selection.isCollapsed) return '';
  const node = selection.anchorNode;
  const inside = !!node && (root.contains(node) || (!(root instanceof Document) && node === root.host));
  return inside ? clip(selection.toString(), ANNOTATION_TEXT_MAX) : '';
}

/** `selectedTextIn` for a frame the host owns, '' for none or a foreign one. */
export function selectedTextInFrame(target: Window | null | undefined): string {
  try {
    const doc = target?.document;
    return doc ? selectedTextIn(doc) : '';
  } catch {
    return '';
  }
}

type AbortReason = Readonly<{ kind: 'cancel' | 'parked' | 'timeout' | 'focus-lost' | 'superseded' }>;
const ABORT_CANCEL: AbortReason = Object.freeze({ kind: 'cancel' });
const ABORT_PARKED: AbortReason = Object.freeze({ kind: 'parked' });
const ABORT_TIMEOUT: AbortReason = Object.freeze({ kind: 'timeout' });
const ABORT_FOCUS_LOST: AbortReason = Object.freeze({ kind: 'focus-lost' });
const ABORT_SUPERSEDED: AbortReason = Object.freeze({ kind: 'superseded' });

interface PendingEntry { info: Readonly<{ path: string; mode: AnnotationMode }>; controller: AbortController }

const responders: AnnotationResponder[] = [];
const pending = new Map<string, PendingEntry>();
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of [...listeners]) {
    try { listener(); } catch (error) { console.error('annotation listener failed', error); }
  }
}

/** Add a mounted view; the returned function removes it. */
export function registerAnnotationResponder(responder: AnnotationResponder): () => void {
  responders.push(responder);
  emit();
  return () => {
    const index = responders.indexOf(responder);
    if (index < 0) return;
    responders.splice(index, 1);
    emit();
  };
}

/** A responder's probe (capable, visible, focused, canInteract) may have changed. */
export function notifyAnnotationResponders(): void {
  emit();
}

/** The views of one cell file, in registration order. */
export function annotationResponders(cellId: string, path: string): AnnotationResponder[] {
  return responders.filter(responder => responder.cellId === cellId && responder.path === path);
}

/** Registry, probe and pending-request changes. */
export function subscribeAnnotationRequests(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

const takesRequests = (responder: AnnotationResponder): boolean => responder.capable() && responder.canInteract();

/** True while some mounted view of (cell, path) would take a request now. */
export function useAnnotationCapable(cellId: string, path: string | null): boolean {
  const read = () => path !== null && annotationResponders(cellId, path).some(takesRequests);
  return useSyncExternalStore(subscribeAnnotationRequests, read, read);
}

/** The cell's pending request (a stable object while it lasts), or null. */
export function pendingAnnotation(cellId: string): { path: string; mode: AnnotationMode } | null {
  return pending.get(cellId)?.info ?? null;
}

/** True while any cell waits on a pick: the scene's Esc then yields to it. */
export function hasPendingAnnotationPick(): boolean {
  for (const entry of pending.values()) if (entry.info.mode === 'pick') return true;
  return false;
}

/** Cancel the cell's pending request; its outcome is `cancelled`. */
export function cancelAnnotationRequest(cellId: string): void {
  pending.get(cellId)?.controller.abort(ABORT_CANCEL);
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Object.prototype.toString.call(value) === '[object Object]';
const clip = (value: string, max: number): string => (value.length > max ? `${value.slice(0, max - 1)}…` : value);

/** A renderer's answer within the host's bounds, or null when unreadable:
 *  the label collapsed and clipped, the text clipped, and the anchor kept
 *  only as a small plain JSON object. */
export function normalizeAnnotationContext(raw: unknown): AnnotationContext | null {
  if (!isRecord(raw)) return null;
  const { label, text, anchor } = raw;
  if (typeof label !== 'string' || typeof text !== 'string') return null;
  const collapsed = label.replace(/\s+/g, ' ').trim();
  if (!collapsed) return null;
  const context: AnnotationContext = { label: clip(collapsed, ANNOTATION_LABEL_MAX), text: clip(text, ANNOTATION_TEXT_MAX) };
  if (isRecord(anchor)) {
    try {
      const json = JSON.stringify(anchor);
      const copy: unknown = json.length <= ANNOTATION_ANCHOR_MAX ? JSON.parse(json) : null;
      if (isRecord(copy)) context.anchor = copy;
    } catch { /* not JSON: the anchor is dropped */ }
  }
  return context;
}

function bestOf(ready: AnnotationResponder[]): AnnotationResponder {
  return ready.filter(r => r.focused()).at(-1) ?? ready.filter(r => r.visible()).at(-1) ?? ready[ready.length - 1];
}

/** The file's best mounted view (capable or not) and its selection now. */
function viewOf(cellId: string, path: string): AnnotationView | null {
  const all = annotationResponders(cellId, path);
  if (!all.length) return null;
  const view = bestOf(all);
  let selection = '';
  try { selection = view.selection(); } catch (error) { console.error('annotation view selection failed', error); }
  return { rendererId: view.rendererId, selection };
}

function chooseFrom(cellId: string, path: string, mode: AnnotationMode, elapsed: number): AnnotationResponder | null {
  const ready = annotationResponders(cellId, path).filter(takesRequests);
  if (!ready.length) return null;
  if (mode === 'pick') {
    const focused = ready.filter(r => r.focused()).at(-1);
    if (focused) return focused;
    if (elapsed < ANNOTATION_FOCUS_WAIT_MS) return null;
  }
  return bestOf(ready);
}

function notReady(cellId: string, path: string): AnnotationUnavailableError {
  const all = annotationResponders(cellId, path);
  if (!all.length) return new AnnotationUnavailableError('not-open', `${path} did not open.`);
  if (all.some(r => r.capable() && !r.canInteract())) return new AnnotationUnavailableError('read-only', `${path} is open read-only here.`);
  return new AnnotationUnavailableError('not-capable', `${path}'s renderer does not take annotations.`);
}

/** Re-evaluated on every emit and every 100 ms until a view takes requests,
 *  the deadline passes or the signal aborts. */
function waitForResponder(cellId: string, path: string, mode: AnnotationMode, signal: AbortSignal, deadlineMs: number): Promise<AnnotationResponder> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    const started = Date.now();
    let done = false;
    const finish = () => {
      done = true;
      clearInterval(tick);
      clearTimeout(deadline);
      unsubscribe();
      signal.removeEventListener('abort', onAbort);
    };
    const evaluate = () => {
      if (done) return;
      const choice = chooseFrom(cellId, path, mode, Date.now() - started);
      if (choice) { finish(); resolve(choice); }
    };
    const onAbort = () => { finish(); reject(signal.reason); };
    const tick = setInterval(evaluate, 100);
    const deadline = setTimeout(() => {
      if (done) return;
      const last = chooseFrom(cellId, path, mode, Number.POSITIVE_INFINITY);
      finish();
      if (last) resolve(last);
      else reject(notReady(cellId, path));
    }, deadlineMs);
    const unsubscribe = subscribeAnnotationRequests(evaluate);
    signal.addEventListener('abort', onAbort, { once: true });
    evaluate();
  });
}

async function chooseResponder(cellId: string, path: string, mode: AnnotationMode, signal: AbortSignal): Promise<AnnotationResponder> {
  if (signal.aborted) throw signal.reason;
  const mounted = annotationResponders(cellId, path).length > 0;
  if (!mounted && !useCollimateStore.getState().locateCell(cellId)?.cell.files.some(file => file.name === path)) {
    throw new AnnotationUnavailableError('not-open', `${path} is not a file of this cell.`);
  }
  // A pick happens in the view the user looks at: bring the file forward and
  // give the cell the main focus, or the view's focus blocker eats the click.
  if (mode === 'pick' || !mounted) revealCellFile(cellId, path, { newTab: true, focus: mode === 'pick' });
  return waitForResponder(cellId, path, mode, signal, mounted ? ANNOTATION_ACK_MS : ANNOTATION_OPEN_WAIT_MS);
}

/** Aborts when the chosen view leaves the registry, and a pick when its
 *  view loses the focus or the visibility it had. */
function watchResponder(responder: AnnotationResponder, mode: AnnotationMode, controller: AbortController): () => void {
  let wasFocused = false, wasVisible = false;
  const check = () => {
    if (controller.signal.aborted) return;
    if (!responders.includes(responder)) { controller.abort(ABORT_PARKED); return; }
    if (mode !== 'pick') return;
    const focused = responder.focused(), visible = responder.visible();
    if ((wasFocused && !focused) || (wasVisible && !visible)) { controller.abort(ABORT_FOCUS_LOST); return; }
    wasFocused = wasFocused || focused;
    wasVisible = wasVisible || visible;
  };
  const unsubscribe = subscribeAnnotationRequests(check);
  check();
  return unsubscribe;
}

function raceAbort<T>(work: () => Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    const onAbort = () => reject(signal.reason);
    signal.addEventListener('abort', onAbort, { once: true });
    let running: Promise<T>;
    try { running = Promise.resolve(work()); } catch (error) { running = Promise.reject(error); }
    running.then(
      value => { signal.removeEventListener('abort', onAbort); resolve(value); },
      error => { signal.removeEventListener('abort', onAbort); reject(error); },
    );
  });
}

function outcomeOf(error: unknown, signal: AbortSignal, view: AnnotationView | null): AnnotationOutcome {
  if (signal.aborted) {
    return signal.reason === ABORT_PARKED
      ? { status: 'unavailable', reason: 'parked', message: "The file's view closed before it answered.", view }
      : { status: 'cancelled' };
  }
  if (error instanceof AnnotationUnavailableError) return { status: 'unavailable', reason: error.reason, message: error.message, view };
  if (error instanceof Error && error.name === ANNOTATION_UNAVAILABLE) return { status: 'unavailable', reason: 'renderer', message: error.message, view };
  return { status: 'error', message: error instanceof Error ? error.message : String(error) };
}

/** Ask the view of a cell file for context. Never rejects: every way it can
 *  end is an outcome. `opts.signal` cancels it; `opts.hint` is a short text
 *  a pick prompt may show. */
export async function requestAnnotation(
  cellId: string, path: string, mode: AnnotationMode, opts: { signal?: AbortSignal; hint?: string } = {},
): Promise<AnnotationOutcome> {
  pending.get(cellId)?.controller.abort(ABORT_SUPERSEDED);
  const controller = new AbortController();
  const { signal } = controller;
  const entry: PendingEntry = { info: Object.freeze({ path, mode }), controller };
  const cleanups: (() => void)[] = [];
  // Any abort ends the pending state at once, before the outcome settles, so
  // the composer's waiting row and the scene's Esc follow it in the same turn.
  signal.addEventListener('abort', () => {
    if (pending.get(cellId) !== entry) return;
    pending.delete(cellId);
    emit();
  }, { once: true });
  pending.set(cellId, entry);
  emit();
  // Read before anything reveals or focuses: the selection the user made.
  const view = viewOf(cellId, path);
  const outer = opts.signal;
  if (outer?.aborted) controller.abort(ABORT_CANCEL);
  else if (outer) {
    const onOuter = () => controller.abort(ABORT_CANCEL);
    outer.addEventListener('abort', onOuter, { once: true });
    cleanups.push(() => outer.removeEventListener('abort', onOuter));
  }
  if (mode === 'pick') {
    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      controller.abort(ABORT_CANCEL);
    };
    window.addEventListener('keydown', onEscape, { capture: true });
    cleanups.push(() => window.removeEventListener('keydown', onEscape, { capture: true }));
  }
  const ceiling = setTimeout(() => controller.abort(ABORT_TIMEOUT), ANNOTATION_PICK_MS);
  cleanups.push(() => clearTimeout(ceiling));
  try {
    const responder = await chooseResponder(cellId, path, mode, signal);
    cleanups.push(watchResponder(responder, mode, controller));
    const request: AnnotationRequest = { id: crypto.randomUUID(), mode, ...(opts.hint ? { hint: opts.hint } : {}) };
    const answer = await raceAbort(() => responder.request(request, signal), signal);
    if (answer === null || answer === undefined) return { status: 'cancelled' };
    const context = normalizeAnnotationContext(answer);
    if (!context) return { status: 'error', message: 'The renderer answered with an unreadable annotation.' };
    return { status: 'ok', path, rendererId: responder.rendererId, context };
  } catch (error) {
    return outcomeOf(error, signal, view ?? viewOf(cellId, path));
  } finally {
    for (const cleanup of cleanups.splice(0)) cleanup();
    if (pending.get(cellId) === entry) {
      pending.delete(cellId);
      emit();
    }
  }
}

/** One request over the sandbox wire to the frame `target`. Accepts answers
 *  only from that window, this origin and this request id; `isCurrent`
 *  false (the host let the frame go) rejects `parked`. However it ends
 *  unanswered, the frame is told ANNOTATION_CANCEL: a pooled frame outlives
 *  the view that asked (parked, or handed to the next one), and its pick
 *  would stay armed; a frame no longer holding the request id ignores it.
 *  Never deduplicated, never re-sent. */
export function requestSandboxAnnotation(
  target: Window, request: AnnotationRequest, signal: AbortSignal, isCurrent: () => boolean,
): Promise<AnnotationContext | null> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    const requestId = request.id;
    const cancel = () => target.postMessage({ type: 'ANNOTATION_CANCEL', requestId }, window.location.origin);
    let ackTimer: ReturnType<typeof setTimeout> | null = setTimeout(() => {
      cancel();
      finish();
      reject(new AnnotationUnavailableError('not-capable', 'The renderer did not answer.'));
    }, ANNOTATION_ACK_MS);
    const ownerCheck = setInterval(() => {
      if (isCurrent()) return;
      cancel();
      finish();
      reject(new AnnotationUnavailableError('parked', 'The file’s view was replaced.'));
    }, 250);
    const onAbort = () => {
      cancel();
      finish();
      reject(signal.reason);
    };
    const answer = (event: MessageEvent) => {
      if (event.source !== target || event.origin !== window.location.origin) return;
      const data: unknown = event.data;
      if (!isRecord(data) || data.requestId !== requestId) return;
      if (data.type === 'ANNOTATION_ACK') {
        if (ackTimer !== null) clearTimeout(ackTimer);
        ackTimer = null;
        return;
      }
      if (data.type !== 'ANNOTATION_RESULT') return;
      finish();
      if (data.unsupported === true) reject(new AnnotationUnavailableError('not-capable', 'The renderer does not take annotations.'));
      else if (typeof data.unavailable === 'string') reject(new AnnotationUnavailableError('renderer', data.unavailable));
      else if (typeof data.error === 'string') reject(new Error(data.error));
      else if (data.cancelled === true) resolve(null);
      else resolve((data.context ?? null) as AnnotationContext | null);
    };
    function finish() {
      if (ackTimer !== null) clearTimeout(ackTimer);
      ackTimer = null;
      clearInterval(ownerCheck);
      window.removeEventListener('message', answer);
      signal.removeEventListener('abort', onAbort);
    }
    window.addEventListener('message', answer);
    signal.addEventListener('abort', onAbort, { once: true });
    target.postMessage({
      type: 'ANNOTATION_REQUEST', requestId, mode: request.mode, ...(request.hint ? { hint: request.hint } : {}),
    }, window.location.origin);
  });
}
