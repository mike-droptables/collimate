// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import {
  ANNOTATION_ACK_MS, ANNOTATION_ANCHOR_MAX, ANNOTATION_FOCUS_WAIT_MS, ANNOTATION_LABEL_MAX, ANNOTATION_OPEN_WAIT_MS,
  ANNOTATION_PICK_MS, ANNOTATION_TEXT_MAX, AnnotationUnavailableError, annotationResponders, cancelAnnotationRequest,
  hasPendingAnnotationPick, normalizeAnnotationContext, notifyAnnotationResponders, pendingAnnotation,
  registerAnnotationResponder, requestAnnotation, selectedTextIn, selectedTextInFrame, subscribeAnnotationRequests, useAnnotationCapable,
  type AnnotationContext, type AnnotationOutcome, type AnnotationRequest, type AnnotationResponder,
} from './rendererAnnotation';

const fixture = vi.hoisted(() => ({ files: ['page.html', 'notes.md'], reveal: vi.fn() }));
vi.mock('@/lib/revealFile', () => ({ revealCellFile: fixture.reveal }));
vi.mock('@/stores/useCollimateStore', () => ({
  useCollimateStore: { getState: () => ({
    locateCell: (cellId: string) => (cellId === 'cell' ? { cell: { files: fixture.files.map(name => ({ name })) } } : null),
  }) },
}));

type Answer = (request: AnnotationRequest, signal: AbortSignal) => Promise<AnnotationContext | null>;
interface Probe { visible: boolean; focused: boolean; capable: boolean; canInteract: boolean; selection: string }
type Fake = AnnotationResponder & { probe: Probe; requests: { request: AnnotationRequest; signal: AbortSignal }[]; off: () => void };

const CONTEXT: AnnotationContext = { label: 'page.html › #sec', text: 'Element `<section>`', anchor: { css: '#sec' } };
const unregisters: (() => void)[] = [];
let views = 0;

/** A registered responder whose probes the test flips (then notifies). */
function responder(probe: Partial<Probe> = {}, answer: Answer = async () => CONTEXT, where: { cellId?: string; path?: string; rendererId?: string } = {}): Fake {
  const state: Probe = { visible: true, focused: false, capable: true, canInteract: true, selection: '', ...probe };
  const fake = {
    cellId: where.cellId ?? 'cell', path: where.path ?? 'page.html', rendererId: where.rendererId ?? 'html', viewKey: `view-${++views}`,
    probe: state, requests: [] as Fake['requests'],
    visible: () => state.visible, focused: () => state.focused, capable: () => state.capable, canInteract: () => state.canInteract,
    selection: () => state.selection,
    request: (request: AnnotationRequest, signal: AbortSignal) => { fake.requests.push({ request, signal }); return answer(request, signal); },
    off: () => {},
  } as Fake;
  fake.off = registerAnnotationResponder(fake);
  unregisters.push(fake.off);
  return fake;
}

function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

/** Lets the request's microtasks (choose, ask) run under fake timers. */
const settle = () => vi.advanceTimersByTimeAsync(0);
const escape = (init: KeyboardEventInit = {}) => {
  const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, ...init });
  window.dispatchEvent(event);
  return event;
};

beforeEach(() => {
  vi.useFakeTimers();
  fixture.files = ['page.html', 'notes.md'];
  fixture.reveal.mockReset();
});
afterEach(async () => {
  for (const cellId of ['cell', 'other']) cancelAnnotationRequest(cellId);
  await settle();
  for (const off of unregisters.splice(0)) off();
  vi.useRealTimers();
});

describe('the responder registry', () => {
  it('lists a file\'s views in registration order, notifies on every change, and unregisters once', () => {
    const heard = vi.fn();
    const off = subscribeAnnotationRequests(heard);
    const first = responder();
    responder({}, undefined, { path: 'notes.md' });
    const third = responder();
    responder({}, undefined, { cellId: 'other' });
    expect(annotationResponders('cell', 'page.html')).toEqual([first, third]);
    expect(heard).toHaveBeenCalledTimes(4);
    notifyAnnotationResponders();
    expect(heard).toHaveBeenCalledTimes(5);
    first.off();
    first.off();
    expect(annotationResponders('cell', 'page.html')).toEqual([third]);
    expect(heard).toHaveBeenCalledTimes(6);
    off();
    third.off();
    expect(heard).toHaveBeenCalledTimes(6);
  });

  it('keeps notifying the others when one listener throws', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    const offBroken = subscribeAnnotationRequests(() => { throw new Error('broken listener'); });
    const heard = vi.fn();
    const off = subscribeAnnotationRequests(heard);
    notifyAnnotationResponders();
    expect(heard).toHaveBeenCalledOnce();
    expect(error).toHaveBeenCalled();
    offBroken(); off(); error.mockRestore();
  });

  it('useAnnotationCapable follows the views of the path that take requests', () => {
    const { result, rerender } = renderHook(({ path }: { path: string | null }) => useAnnotationCapable('cell', path), { initialProps: { path: 'page.html' as string | null } });
    expect(result.current).toBe(false);
    let view!: Fake;
    act(() => { view = responder({ capable: false }); });
    expect(result.current).toBe(false);
    act(() => { view.probe.capable = true; notifyAnnotationResponders(); });
    expect(result.current).toBe(true);
    act(() => { view.probe.canInteract = false; notifyAnnotationResponders(); });
    expect(result.current).toBe(false);
    act(() => { view.probe.canInteract = true; notifyAnnotationResponders(); });
    rerender({ path: null });
    expect(result.current).toBe(false);
    rerender({ path: 'page.html' });
    act(() => { view.off(); });
    expect(result.current).toBe(false);
  });
});

describe('routing', () => {
  it('a pick takes the focused view, the last of them, and skips views that take no requests', async () => {
    const focusedEarly = responder({ focused: true });
    const focused = responder({ focused: true });
    responder({ focused: true, capable: false });
    responder({ focused: true, canInteract: false });
    const outcome = await requestAnnotation('cell', 'page.html', 'pick');
    expect(outcome).toEqual({ status: 'ok', path: 'page.html', rendererId: 'html', context: CONTEXT });
    expect(focused.requests).toHaveLength(1);
    expect(focusedEarly.requests).toHaveLength(0);
    // A pick happens where the user looks: the file comes forward, focused.
    expect(fixture.reveal).toHaveBeenCalledWith('cell', 'page.html', { newTab: true, focus: true });
  });

  it('a pick waits for the focus it asked for, then takes the best view', async () => {
    const visible = responder({ visible: true });
    responder({ visible: false });
    const pending = requestAnnotation('cell', 'page.html', 'pick');
    await vi.advanceTimersByTimeAsync(ANNOTATION_FOCUS_WAIT_MS - 200);
    expect(visible.requests).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(300);
    expect(visible.requests).toHaveLength(1);
    expect(await pending).toMatchObject({ status: 'ok' });
  });

  it('a pick takes the view that gains the focus while it waits', async () => {
    const other = responder({ visible: true });
    const target = responder({ visible: false });
    const pending = requestAnnotation('cell', 'page.html', 'pick');
    await settle();
    target.probe.focused = true;
    target.probe.visible = true;
    notifyAnnotationResponders();
    expect(await pending).toMatchObject({ status: 'ok' });
    expect(target.requests).toHaveLength(1);
    expect(other.requests).toHaveLength(0);
  });

  it('a selection asks at once: focused, else visible, else the latest view, and opens nothing', async () => {
    const a = responder({ visible: false });
    const b = responder({ visible: true });
    const c = responder({ visible: false });
    await requestAnnotation('cell', 'page.html', 'selection');
    expect(b.requests).toHaveLength(1);
    a.probe.focused = true;
    await requestAnnotation('cell', 'page.html', 'selection');
    expect(a.requests).toHaveLength(1);
    a.probe.focused = false;
    b.probe.visible = false;
    await requestAnnotation('cell', 'page.html', 'selection');
    expect(c.requests).toHaveLength(1);
    expect(fixture.reveal).not.toHaveBeenCalled();
  });

  it('opens a cell file with no view and asks the view that mounts', async () => {
    const pick = requestAnnotation('cell', 'page.html', 'pick', { hint: 'Click it' });
    await settle();
    expect(fixture.reveal).toHaveBeenCalledWith('cell', 'page.html', { newTab: true, focus: true });
    await vi.advanceTimersByTimeAsync(1500);
    const view = responder({ focused: true });
    expect(await pick).toMatchObject({ status: 'ok', rendererId: 'html' });
    expect(view.requests[0].request).toEqual({ id: expect.any(String), mode: 'pick', hint: 'Click it' });

    fixture.reveal.mockReset();
    view.off();
    const selection = requestAnnotation('cell', 'notes.md', 'selection');
    await settle();
    expect(fixture.reveal).toHaveBeenCalledWith('cell', 'notes.md', { newTab: true, focus: false });
    const editor = responder({}, undefined, { path: 'notes.md', rendererId: 'milkdown' });
    expect(await selection).toMatchObject({ status: 'ok', path: 'notes.md', rendererId: 'milkdown' });
    expect(editor.requests[0].request).toEqual({ id: expect.any(String), mode: 'selection' });
  });

  it('answers unavailable when the file is not the cell\'s, did not open, takes no requests or is read-only', async () => {
    expect(await requestAnnotation('cell', 'gone.html', 'pick')).toEqual({
      status: 'unavailable', reason: 'not-open', message: 'gone.html is not a file of this cell.', view: null });
    expect(fixture.reveal).not.toHaveBeenCalled();

    const opening = requestAnnotation('cell', 'page.html', 'pick');
    await vi.advanceTimersByTimeAsync(ANNOTATION_OPEN_WAIT_MS);
    expect(await opening).toEqual({ status: 'unavailable', reason: 'not-open', message: 'page.html did not open.', view: null });

    const incapable = responder({ capable: false, focused: true });
    const silent = requestAnnotation('cell', 'page.html', 'selection');
    await vi.advanceTimersByTimeAsync(ANNOTATION_ACK_MS);
    expect(await silent).toEqual({ status: 'unavailable', reason: 'not-capable', message: "page.html's renderer does not take annotations.",
      view: { rendererId: 'html', selection: '' } });

    incapable.probe.capable = true;
    incapable.probe.canInteract = false;
    const locked = requestAnnotation('cell', 'page.html', 'pick');
    await vi.advanceTimersByTimeAsync(ANNOTATION_ACK_MS);
    expect(await locked).toEqual({ status: 'unavailable', reason: 'read-only', message: 'page.html is open read-only here.',
      view: { rendererId: 'html', selection: '' } });
  });

  it('an unavailable outcome carries the view and the selection it had when asked, before the request focused it', async () => {
    const view = responder({ capable: false, focused: false, selection: 'the selected words' });
    const request = requestAnnotation('cell', 'page.html', 'pick');
    // The reveal's focus, or the user's next click, may change the selection: the one at the press is what rides.
    view.probe.selection = '';
    view.probe.focused = true;
    await vi.advanceTimersByTimeAsync(ANNOTATION_ACK_MS);
    expect(await request).toEqual({ status: 'unavailable', reason: 'not-capable', message: "page.html's renderer does not take annotations.",
      view: { rendererId: 'html', selection: 'the selected words' } });
    // A pick a second view answers in the user's view takes none of it.
    const picker = responder({ focused: true, selection: 'ignored' });
    expect(await requestAnnotation('cell', 'page.html', 'pick')).toEqual({ status: 'ok', path: 'page.html', rendererId: 'html', context: CONTEXT });
    picker.off();
    // A file whose view mounts only after the request was made is read at the failure.
    view.off();
    const late = requestAnnotation('cell', 'page.html', 'pick');
    await settle();
    responder({ capable: false, selection: 'late words' }, undefined, { rendererId: 'milkdown' });
    await vi.advanceTimersByTimeAsync(ANNOTATION_OPEN_WAIT_MS);
    expect(await late).toEqual({ status: 'unavailable', reason: 'not-capable', message: "page.html's renderer does not take annotations.",
      view: { rendererId: 'milkdown', selection: 'late words' } });
    // A probe that throws reads as no selection.
    const broken = responder({ capable: false, focused: true });
    broken.selection = () => { throw new Error('no window'); };
    const silenced = vi.spyOn(console, 'error').mockImplementation(() => {});
    const thrown = requestAnnotation('cell', 'page.html', 'pick');
    await vi.advanceTimersByTimeAsync(ANNOTATION_ACK_MS);
    expect(await thrown).toMatchObject({ status: 'unavailable', view: { rendererId: 'html', selection: '' } });
    silenced.mockRestore();
  });
});

describe('pending requests and cancellation', () => {
  it('holds one request per cell: a second cancels the first and aborts its view\'s signal', async () => {
    const answer = deferred<AnnotationContext | null>();
    const view = responder({ focused: true }, () => answer.promise);
    const first = requestAnnotation('cell', 'page.html', 'pick');
    await settle();
    const info = pendingAnnotation('cell');
    expect(info).toEqual({ path: 'page.html', mode: 'pick' });
    expect(pendingAnnotation('cell')).toBe(info);
    expect(hasPendingAnnotationPick()).toBe(true);
    const second = requestAnnotation('cell', 'page.html', 'selection');
    expect(await first).toEqual({ status: 'cancelled' });
    expect(view.requests[0].signal.aborted).toBe(true);
    expect(pendingAnnotation('cell')).toEqual({ path: 'page.html', mode: 'selection' });
    expect(hasPendingAnnotationPick()).toBe(false);
    answer.resolve(CONTEXT);
    expect(await second).toMatchObject({ status: 'ok', context: CONTEXT });
    expect(view.requests[1].request.mode).toBe('selection');
    expect(pendingAnnotation('cell')).toBeNull();
  });

  it('a bare Escape on the window cancels a pick at once; a modified one does not', async () => {
    const view = responder({ focused: true }, (_request, signal) => new Promise(resolve => signal.addEventListener('abort', () => resolve(null))));
    const pick = requestAnnotation('cell', 'page.html', 'pick');
    await settle();
    const modified = escape({ shiftKey: true });
    expect(modified.defaultPrevented).toBe(false);
    expect(view.requests[0].signal.aborted).toBe(false);
    const bare = escape();
    expect(bare.defaultPrevented).toBe(true);
    expect(hasPendingAnnotationPick()).toBe(false);
    expect(await pick).toEqual({ status: 'cancelled' });
    expect(escape().defaultPrevented).toBe(false);
  });

  it('Escape leaves a selection alone', async () => {
    const answer = deferred<AnnotationContext | null>();
    responder({}, () => answer.promise);
    const selection = requestAnnotation('cell', 'page.html', 'selection');
    await settle();
    expect(escape().defaultPrevented).toBe(false);
    answer.resolve(CONTEXT);
    expect(await selection).toMatchObject({ status: 'ok' });
  });

  it('the caller\'s signal and cancelAnnotationRequest cancel, even a view that ignores its signal', async () => {
    responder({ focused: true }, () => new Promise(() => {}));
    const controller = new AbortController();
    const asked = requestAnnotation('cell', 'page.html', 'pick', { signal: controller.signal });
    await settle();
    controller.abort();
    expect(await asked).toEqual({ status: 'cancelled' });

    const again = requestAnnotation('cell', 'page.html', 'pick');
    await settle();
    cancelAnnotationRequest('cell');
    expect(await again).toEqual({ status: 'cancelled' });

    fixture.reveal.mockReset();
    const aborted = new AbortController();
    aborted.abort();
    expect(await requestAnnotation('cell', 'page.html', 'pick', { signal: aborted.signal })).toEqual({ status: 'cancelled' });
    expect(fixture.reveal).not.toHaveBeenCalled();
  });

  it('cancels while it waits for a view to mount', async () => {
    const opening = requestAnnotation('cell', 'page.html', 'pick');
    await vi.advanceTimersByTimeAsync(1000);
    cancelAnnotationRequest('cell');
    expect(await opening).toEqual({ status: 'cancelled' });
  });

  it('a view that goes away answers parked', async () => {
    const view = responder({ focused: true }, () => new Promise(() => {}));
    const pick = requestAnnotation('cell', 'page.html', 'pick');
    await settle();
    view.off();
    expect(await pick).toEqual({ status: 'unavailable', reason: 'parked', message: "The file's view closed before it answered.",
      view: { rendererId: 'html', selection: '' } });
  });

  it('a pick whose view loses the focus or the visibility it had is cancelled', async () => {
    const view = responder({ focused: true }, () => new Promise(() => {}));
    const pick = requestAnnotation('cell', 'page.html', 'pick');
    await settle();
    view.probe.focused = false;
    notifyAnnotationResponders();
    expect(await pick).toEqual({ status: 'cancelled' });

    view.probe.focused = true;
    const hidden = requestAnnotation('cell', 'page.html', 'pick');
    await settle();
    view.probe.visible = false;
    notifyAnnotationResponders();
    expect(await hidden).toEqual({ status: 'cancelled' });
  });

  it('a view that never had the focus keeps its pick when the focus stays elsewhere', async () => {
    const answer = deferred<AnnotationContext | null>();
    const view = responder({ visible: true }, () => answer.promise);
    const pick = requestAnnotation('cell', 'page.html', 'pick');
    await vi.advanceTimersByTimeAsync(ANNOTATION_FOCUS_WAIT_MS + 100);
    expect(view.requests).toHaveLength(1);
    notifyAnnotationResponders();
    expect(view.requests[0].signal.aborted).toBe(false);
    answer.resolve(CONTEXT);
    expect(await pick).toMatchObject({ status: 'ok' });
  });

  it('the five-minute ceiling cancels a request the user never finishes, a selection too', async () => {
    responder({ focused: true }, () => new Promise(() => {}));
    const pick = requestAnnotation('cell', 'page.html', 'selection');
    expect(ANNOTATION_PICK_MS).toBe(300_000);
    await vi.advanceTimersByTimeAsync(300_000 - 1);
    expect(pendingAnnotation('cell')).not.toBeNull();
    await vi.advanceTimersByTimeAsync(1);
    expect(await pick).toEqual({ status: 'cancelled' });
    expect(pendingAnnotation('cell')).toBeNull();
  });

  it('requests on two cells stay apart', async () => {
    const a = deferred<AnnotationContext | null>();
    responder({ focused: true }, () => a.promise);
    fixture.files = ['page.html'];
    responder({}, async () => CONTEXT, { cellId: 'other' });
    const first = requestAnnotation('cell', 'page.html', 'pick');
    await settle();
    expect(await requestAnnotation('other', 'page.html', 'selection')).toMatchObject({ status: 'ok' });
    expect(pendingAnnotation('cell')).not.toBeNull();
    a.resolve(CONTEXT);
    expect(await first).toMatchObject({ status: 'ok' });
  });
});

describe('outcomes', () => {
  const run = async (answer: Answer): Promise<AnnotationOutcome> => {
    const view = responder({ focused: true }, answer);
    try { return await requestAnnotation('cell', 'page.html', 'pick'); } finally { view.off(); }
  };

  it('maps every answer to its outcome', async () => {
    expect(await run(async () => CONTEXT)).toEqual({ status: 'ok', path: 'page.html', rendererId: 'html', context: CONTEXT });
    expect(await run(async () => null)).toEqual({ status: 'cancelled' });
    expect(await run(async () => ({ label: 5 }) as unknown as AnnotationContext)).toEqual({
      status: 'error', message: 'The renderer answered with an unreadable annotation.' });
    expect(await run(async () => { throw Object.assign(new Error('This page answers element picks only.'), { name: 'AnnotationUnavailable' }); }))
      .toEqual({ status: 'unavailable', reason: 'renderer', message: 'This page answers element picks only.', view: { rendererId: 'html', selection: '' } });
    expect(await run(async () => { throw new AnnotationUnavailableError('parked', 'The file’s view was replaced.'); }))
      .toEqual({ status: 'unavailable', reason: 'parked', message: 'The file’s view was replaced.', view: { rendererId: 'html', selection: '' } });
    expect(await run(async () => { throw new Error('boom'); })).toEqual({ status: 'error', message: 'boom' });
    expect(await run(async () => { throw 'plain'; })).toEqual({ status: 'error', message: 'plain' });
    expect(await run(() => { throw new Error('sync boom'); })).toEqual({ status: 'error', message: 'sync boom' });
  });

  it('normalizes what it hands on within the host bounds', async () => {
    const long = 'x'.repeat(ANNOTATION_TEXT_MAX + 5);
    const outcome = await run(async () => ({ label: `  a\n  ${'b'.repeat(200)} `, text: long, anchor: { css: 'li' } }));
    expect(outcome.status).toBe('ok');
    if (outcome.status !== 'ok') return;
    expect(outcome.context.label).toHaveLength(ANNOTATION_LABEL_MAX);
    expect(outcome.context.label.startsWith('a b')).toBe(true);
    expect(outcome.context.label.endsWith('…')).toBe(true);
    expect(outcome.context.text).toHaveLength(ANNOTATION_TEXT_MAX);
    expect(outcome.context.anchor).toEqual({ css: 'li' });
  });
});

describe('normalizeAnnotationContext', () => {
  it('refuses what is not a labelled text', () => {
    for (const raw of [null, undefined, 'text', 3, [], { label: 'x' }, { text: 'x' }, { label: 1, text: 'x' }, { label: ' \n ', text: 'x' }]) {
      expect(normalizeAnnotationContext(raw)).toBeNull();
    }
  });

  it('keeps an anchor only as a small plain JSON object, as a copy', () => {
    const anchor = { css: '#a', nested: { n: 1 } };
    const kept = normalizeAnnotationContext({ label: 'L', text: 'T', anchor });
    expect(kept).toEqual({ label: 'L', text: 'T', anchor });
    expect(kept?.anchor).not.toBe(anchor);
    const big = { css: 'x'.repeat(ANNOTATION_ANCHOR_MAX) };
    expect(normalizeAnnotationContext({ label: 'L', text: 'T', anchor: big })).toEqual({ label: 'L', text: 'T' });
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(normalizeAnnotationContext({ label: 'L', text: 'T', anchor: cyclic })).toEqual({ label: 'L', text: 'T' });
    expect(normalizeAnnotationContext({ label: 'L', text: 'T', anchor: ['a'] })).toEqual({ label: 'L', text: 'T' });
    expect(normalizeAnnotationContext({ label: 'L', text: 'T', anchor: { toJSON: () => 5 } })).toEqual({ label: 'L', text: 'T' });
    expect(normalizeAnnotationContext({ label: 'L', text: 'T', anchor: null })).toEqual({ label: 'L', text: 'T' });
  });

  it('clips at the bounds and leaves what fits', () => {
    const label = 'l'.repeat(ANNOTATION_LABEL_MAX);
    expect(normalizeAnnotationContext({ label, text: '' })?.label).toBe(label);
    expect(normalizeAnnotationContext({ label: `${label}m`, text: '' })?.label).toBe(`${'l'.repeat(ANNOTATION_LABEL_MAX - 1)}…`);
  });
});

describe('selectedTextIn', () => {
  type FakeRoot = { getSelection?: () => Selection | null; contains: (n: Node) => boolean; host?: Element; ownerDocument?: Document };
  const selection = (fields: Partial<Selection>): Selection => ({ isCollapsed: false, anchorNode: null, toString: () => '', ...fields }) as Selection;

  it('reads the selection inside a root, clipped, and nothing for a collapsed or foreign one', () => {
    const inside = document.createElement('p');
    const outside = document.createElement('p');
    const root = { getSelection: () => selection({ anchorNode: inside, toString: () => 'picked words' }), contains: (n: Node) => n === inside } as FakeRoot;
    expect(selectedTextIn(root as unknown as ShadowRoot)).toBe('picked words');
    root.getSelection = () => selection({ anchorNode: outside, toString: () => 'elsewhere' });
    expect(selectedTextIn(root as unknown as ShadowRoot)).toBe('');
    root.getSelection = () => selection({ anchorNode: inside, isCollapsed: true, toString: () => 'caret' });
    expect(selectedTextIn(root as unknown as ShadowRoot)).toBe('');
    root.getSelection = () => null;
    expect(selectedTextIn(root as unknown as ShadowRoot)).toBe('');
    root.getSelection = () => selection({ anchorNode: inside, toString: () => 'x'.repeat(ANNOTATION_TEXT_MAX + 5) });
    expect(selectedTextIn(root as unknown as ShadowRoot)).toHaveLength(ANNOTATION_TEXT_MAX);
  });

  it('a shadow root without a selection of its own reads the document’s, retargeted to its host', () => {
    const host = document.createElement('div');
    document.body.append(host);
    const root = host.attachShadow({ mode: 'open' });
    const inner = document.createElement('p');
    root.append(inner);
    const getSelection = vi.spyOn(document, 'getSelection');
    try {
      getSelection.mockReturnValue(selection({ anchorNode: host, toString: () => 'shadow words' }));
      expect(selectedTextIn(root)).toBe('shadow words');
      getSelection.mockReturnValue(selection({ anchorNode: inner, toString: () => 'inner words' }));
      expect(selectedTextIn(root)).toBe('inner words');
      getSelection.mockReturnValue(selection({ anchorNode: document.body, toString: () => 'page words' }));
      expect(selectedTextIn(root)).toBe('');
    } finally {
      getSelection.mockRestore();
      host.remove();
    }
  });

  it('a frame’s document is read the same way; no frame or a foreign one reads as nothing', () => {
    const doc = document.implementation.createHTMLDocument();
    const node = doc.createElement('p');
    doc.body.append(node);
    doc.getSelection = () => selection({ anchorNode: node, toString: () => 'frame words' });
    expect(selectedTextInFrame({ document: doc } as unknown as Window)).toBe('frame words');
    expect(selectedTextInFrame(null)).toBe('');
    expect(selectedTextInFrame(undefined)).toBe('');
    const foreign = {} as Window;
    Object.defineProperty(foreign, 'document', { get() { throw new DOMException('Blocked a frame', 'SecurityError'); } });
    expect(selectedTextInFrame(foreign)).toBe('');
  });
});
