import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/nativeRendererCache', () => ({ getCachedRenderer: vi.fn() }));
vi.mock('@/lib/revealFile', () => ({ revealCellFile: vi.fn() }));

import { NativeRenderer } from './NativeRenderer';
import { getCachedRenderer } from '@/lib/nativeRendererCache';
import {
  annotationResponders, cancelAnnotationRequest, requestAnnotation,
  type AnnotationContext, type AnnotationHandler, type AnnotationRequest,
} from '@/lib/rendererAnnotation';

type AnnotationApi = { onAnnotationRequest(handler: unknown): () => void; ready?(): void };
const CONTEXT: AnnotationContext = { label: 'notes.md › Intro', text: 'The intro' };

/** Mounts a native renderer whose module hands the test its api. */
async function mounted(props: { cellId?: string; readOnly?: boolean; isActiveFocus?: boolean } = {}) {
  let api: AnnotationApi | null = null;
  let root: Element | null = null;
  vi.mocked(getCachedRenderer).mockResolvedValueOnce({
    module: { mount: (el: Element, a: AnnotationApi) => { root = el; api = a; a.ready?.(); } },
    blobUrl: null, moduleUrl: 'blob:annotation-test',
  } as never);
  const view = render(<NativeRenderer rendererId="notes-view" fileContent="" fileName="notes.md"
    cellId={'cellId' in props ? props.cellId : 'cell-1'} readOnly={props.readOnly} isActiveFocus={props.isActiveFocus ?? true} />);
  await waitFor(() => expect(api).not.toBeNull());
  return { api: api!, view, root: root! };
}

afterEach(() => { cancelAnnotationRequest('cell-1'); cleanup(); vi.clearAllMocks(); });

describe('NativeRenderer api.onAnnotationRequest', () => {
  it('its view reads the selection inside its shadow root, and nothing selected elsewhere', async () => {
    const { root, view } = await mounted();
    const [responder] = annotationResponders('cell-1', 'notes.md');
    const text = document.createElement('p');
    root.append(text);
    const getSelection = vi.spyOn(document, 'getSelection');
    try {
      expect(responder.selection()).toBe('');
      getSelection.mockReturnValue({ isCollapsed: false, anchorNode: text, toString: () => 'The intro' } as unknown as Selection);
      expect(responder.selection()).toBe('The intro');
      getSelection.mockReturnValue({ isCollapsed: false, anchorNode: document.body, toString: () => 'elsewhere' } as unknown as Selection);
      expect(responder.selection()).toBe('');
      getSelection.mockReturnValue({ isCollapsed: false, anchorNode: text, toString: () => 'gone' } as unknown as Selection);
      view.unmount();
      expect(responder.selection()).toBe('');
    } finally {
      getSelection.mockRestore();
    }
  });

  it('registers its view, becomes capable on subscribe and answers through the latest handler', async () => {
    const { api, view } = await mounted();
    const [responder] = annotationResponders('cell-1', 'notes.md');
    expect([responder.rendererId, responder.capable(), responder.canInteract(), responder.focused(), responder.visible()])
      .toEqual(['notes-view', false, true, true, true]);
    const seen: AnnotationRequest[] = [];
    const first: AnnotationHandler = async request => { seen.push(request); return null; };
    const second: AnnotationHandler = async request => { seen.push(request); return CONTEXT; };
    const offFirst = api.onAnnotationRequest(first);
    expect(responder.capable()).toBe(true);
    api.onAnnotationRequest(second);
    // The first handler's withdrawal is stale: the second one stays.
    offFirst();
    expect(responder.capable()).toBe(true);
    await expect(requestAnnotation('cell-1', 'notes.md', 'selection')).resolves.toEqual({
      status: 'ok', path: 'notes.md', rendererId: 'notes-view', context: CONTEXT });
    expect(seen).toEqual([{ id: expect.any(String), mode: 'selection' }]);
    expect(api.onAnnotationRequest('not a function')).toBeTypeOf('function');
    view.unmount();
    expect(annotationResponders('cell-1', 'notes.md')).toEqual([]);
  });

  it('a cancelled request aborts the handler\'s signal, and withdrawing the handler aborts its jobs', async () => {
    const { api } = await mounted();
    const signals: AbortSignal[] = [];
    const off = api.onAnnotationRequest((_request: AnnotationRequest, signal: AbortSignal) => {
      signals.push(signal);
      return new Promise(() => {});
    });
    const pick = requestAnnotation('cell-1', 'notes.md', 'pick');
    await waitFor(() => expect(signals).toHaveLength(1));
    cancelAnnotationRequest('cell-1');
    await expect(pick).resolves.toEqual({ status: 'cancelled' });
    expect(signals[0].aborted).toBe(true);

    const [responder] = annotationResponders('cell-1', 'notes.md');
    const job = responder.request({ id: 'direct', mode: 'pick' }, new AbortController().signal);
    await waitFor(() => expect(signals).toHaveLength(2));
    off();
    expect(signals[1].aborted).toBe(true);
    expect(responder.capable()).toBe(false);
    await expect(responder.request({ id: 'after', mode: 'pick' }, new AbortController().signal)).rejects.toMatchObject({
      name: 'AnnotationUnavailable', reason: 'not-capable' });
    void job;
  });

  it('a read-only view takes no requests, and a view outside a cell registers none', async () => {
    await mounted({ readOnly: true });
    expect(annotationResponders('cell-1', 'notes.md').map(r => r.canInteract())).toEqual([false]);
    cleanup();
    await mounted({ cellId: undefined });
    expect(annotationResponders('', 'notes.md')).toEqual([]);
    expect(annotationResponders('cell-1', 'notes.md')).toEqual([]);
  });
});
