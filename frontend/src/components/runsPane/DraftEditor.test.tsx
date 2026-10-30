// The composer's editor: the channel's .md renderer (native, pooled frame,
// or unpooled in the full-window view) over an in-memory draft under a root
// file name the cell does not hold; the textarea stand-in; text() after a
// flush; focus; the pool woken on every commit; concealed under the cell's
// own confirm or drags.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { createRef } from 'react';

type HostProps = Record<string, unknown> & { onContentChange: (b: string, v?: unknown) => void; onSnapshotChange: (s: unknown, v?: unknown) => void; onGetContent: () => Promise<string> };
const hosts = vi.hoisted(() => ({
    last: null as HostProps | null, kind: '',
    fake: (kind: string) => (props: HostProps) => { hosts.last = props; hosts.kind = kind; return null; },
}));
vi.mock('@/components/NativeRenderer', () => ({ NativeRenderer: hosts.fake('native') }));
vi.mock('@/components/SandboxIframe', () => ({
    SandboxIframe: (props: HostProps) => {
        hosts.last = props;
        hosts.kind = 'sandbox';
        return <ConcealProbe kind="sandbox" />;
    },
}));
vi.mock('@/components/StandaloneSandboxIframe', () => ({ StandaloneSandboxIframe: hosts.fake('standalone') }));
const renderer = vi.hoisted(() => ({ value: { renderer: null as unknown, error: null as string | null } }));
vi.mock('@/hooks/useRendererId', () => ({ useRendererId: () => renderer.value }));
const flush = vi.hoisted(() => ({ editors: vi.fn(async () => {}) }));
vi.mock('@/lib/rendererFlush', async (importOriginal) => ({ ...(await importOriginal<object>()), flushRendererEditors: flush.editors }));
const focusing = vi.hoisted(() => ({ result: false, calls: 0 }));
vi.mock('@/lib/selectionFocus', async (importOriginal) => ({ ...(await importOriginal<object>()), focusEditorIn: () => { focusing.calls++; return focusing.result; } }));
const pool = vi.hoisted(() => ({ invalidate: vi.fn() }));
vi.mock('@/contexts/GuestPoolContext', async (importOriginal) => {
    const actual = await importOriginal<typeof import('@/contexts/GuestPoolContext')>();
    return { ...actual, useGuestPool: () => ({ ...actual.useGuestPool(), invalidate: pool.invalidate }) };
});

import { usePooledIframeConcealed } from '@/contexts/RendererConcealContext';
import { DraftEditor, type DraftEditorHandle } from './DraftEditor';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { CELL_ID, seedPaneStore } from '@/test/annotationFixtures';

function ConcealProbe({ kind }: { kind: string }) {
    const concealed = usePooledIframeConcealed();
    return <div data-testid={`host-${kind}`} data-conceal={String(concealed)} />;
}

const MILKDOWN = { id: 'milkdown', name: 'milkdown', extensions: ['.md'], supportsDiff: false, liveRefresh: true, embed: 'iframe', contentHash: 'h1' };
const mount = (props: Partial<Parameters<typeof DraftEditor>[0]> = {}) => {
    const ref = createRef<DraftEditorHandle>();
    const onText = vi.fn();
    const view = render(<DraftEditor ref={ref} cellId={CELL_ID} channel="ch" initialText="hello" onText={onText} active fullscreen={false} {...props} />);
    return { ref, onText, ...view };
};

beforeEach(() => {
    hosts.last = null;
    renderer.value = { renderer: MILKDOWN, error: null };
    flush.editors.mockClear();
    pool.invalidate.mockClear();
    focusing.result = false;
    focusing.calls = 0;
    seedPaneStore({ cellCache: { [CELL_ID]: { files: [{ name: '.collimate-message.md' }, { name: 'page.html' }] } } });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('DraftEditor', () => {
    it('mounts the .md renderer in a pooled frame over the draft, under a name the cell does not hold', () => {
        mount();
        expect(hosts.kind).toBe('sandbox');
        expect(hosts.last).toMatchObject({
            fileName: '.collimate-message-2.md', fileContent: 'hello', fileHash: '', rendererId: 'milkdown', rendererHash: 'h1',
            isActiveFocus: true, cellId: CELL_ID, channel: 'ch',
        });
        expect(hosts.last!.documentSnapshot).toMatchObject({ content: 'hello', editSeq: 0, materializedSeq: 0, viewAcks: {} });
        expect(JSON.parse(hosts.last!.documentId as string)).toEqual(['message-draft', 'ch', CELL_ID, expect.any(String), 'milkdown', 'h1', 'pane']);
        expect(hosts.last).not.toHaveProperty('readOnly');
    });

    it('the full-window view takes an unpooled frame; a native renderer mounts natively', () => {
        mount({ fullscreen: true });
        expect(hosts.kind).toBe('standalone');
        expect(JSON.parse(hosts.last!.documentId as string).at(-1)).toBe('fullscreen');
        cleanup();
        renderer.value = { renderer: { ...MILKDOWN, id: 'text', embed: 'native' }, error: null };
        mount();
        expect(hosts.kind).toBe('native');
        expect(hosts.last).toMatchObject({ rendererId: 'text', fileContent: 'hello', fileName: '.collimate-message-2.md' });
    });

    it('an edit follows the view\'s acks and mirrors the text out; a snapshot edit mirrors once derived', async () => {
        const { onText } = mount();
        act(() => hosts.last!.onContentChange('hello there', { viewId: 'v', viewSeq: 1 }));
        expect(onText).toHaveBeenCalledWith('hello there');
        expect(hosts.last).toMatchObject({ fileContent: 'hello there' });
        expect(hosts.last!.documentSnapshot).toMatchObject({ viewAcks: { v: 1 }, editSeq: 1 });
        act(() => hosts.last!.onContentChange('stale', { viewId: 'v', viewSeq: 1 }));
        expect(onText).toHaveBeenCalledTimes(1);
        expect(await hosts.last!.onGetContent()).toBe('hello there');
    });

    it('text() flushes the editor first, then reads the draft; reset() empties it in place', async () => {
        const { ref, onText } = mount();
        act(() => hosts.last!.onContentChange('typed', { viewId: 'v', viewSeq: 1 }));
        await expect(ref.current!.text()).resolves.toBe('typed');
        expect(flush.editors).toHaveBeenCalledWith(CELL_ID, '.collimate-message-2.md');
        const owner = (hosts.last!.documentSnapshot as { ownerId: string }).ownerId;
        act(() => ref.current!.reset());
        expect(hosts.last).toMatchObject({ fileContent: '' });
        expect(hosts.last!.documentSnapshot).toMatchObject({ ownerId: owner, content: '', viewAcks: { v: 1 } });
        expect(onText).toHaveBeenLastCalledWith('');
    });

    it('stands in a textarea while there is no renderer, on an error, and for the binary floor', async () => {
        renderer.value = { renderer: null, error: null };
        const { ref, onText } = mount();
        const box = screen.getByTestId('composer-fallback') as HTMLTextAreaElement;
        expect(box.value).toBe('hello');
        fireEvent.change(box, { target: { value: 'plain' } });
        expect(onText).toHaveBeenCalledWith('plain');
        await expect(ref.current!.text()).resolves.toBe('plain');
        expect(ref.current!.focus()).toBe(true);
        expect(document.activeElement).toBe(box);
        cleanup();
        renderer.value = { renderer: MILKDOWN, error: 'registry down' };
        mount();
        expect(screen.getByTestId('composer-fallback')).toBeInTheDocument();
        cleanup();
        renderer.value = { renderer: { ...MILKDOWN, id: 'binary' }, error: null };
        mount();
        expect(screen.getByTestId('composer-fallback')).toBeInTheDocument();
    });

    it('focus() hands the keys to the mounted editor, trying again on later frames while it mounts', () => {
        vi.useFakeTimers({ toFake: ['requestAnimationFrame'] });
        const { ref } = mount();
        expect(ref.current!.focus()).toBe(false);
        expect(focusing.calls).toBe(1);
        act(() => { vi.advanceTimersByTime(16 * 3); });
        expect(focusing.calls).toBeGreaterThan(2);
        focusing.result = true;
        const before = focusing.calls;
        act(() => { vi.advanceTimersByTime(16 * 40); });
        expect(focusing.calls).toBe(before + 1);
        focusing.result = false;
        const again = focusing.calls;
        ref.current!.focus();
        act(() => { vi.advanceTimersByTime(16 * 60); });
        expect(focusing.calls).toBe(again + 1 + 30);
        expect(ref.current!.focus.length).toBe(0);
    });

    it('wakes the guest pool on every commit', () => {
        const { rerender } = mount();
        const after = pool.invalidate.mock.calls.length;
        expect(after).toBeGreaterThan(0);
        rerender(<DraftEditor cellId={CELL_ID} channel="ch" initialText="hello" onText={() => {}} active={false} fullscreen={false} />);
        expect(pool.invalidate.mock.calls.length).toBeGreaterThan(after);
    });

    it('conceals its pooled frame while the cell\'s confirm or a drag in the cell covers the pane', () => {
        mount();
        expect(screen.getByTestId('host-sandbox')).toHaveAttribute('data-conceal', 'false');
        act(() => useCollimateStore.setState({ confirmDialog: { kind: 'confirm', cellId: CELL_ID } as never }));
        expect(screen.getByTestId('host-sandbox')).toHaveAttribute('data-conceal', 'true');
        act(() => useCollimateStore.setState({ confirmDialog: { kind: 'confirm', cellId: 'other' } as never }));
        expect(screen.getByTestId('host-sandbox')).toHaveAttribute('data-conceal', 'false');
        act(() => useCollimateStore.setState({ dockDrag: { cellId: CELL_ID, pane: 'runs', side: null } }));
        expect(screen.getByTestId('host-sandbox')).toHaveAttribute('data-conceal', 'true');
        act(() => useCollimateStore.setState({ dockDrag: null, tabDrag: { cellId: CELL_ID, leafId: 'l', tabId: 't' } }));
        expect(screen.getByTestId('host-sandbox')).toHaveAttribute('data-conceal', 'true');
    });
});
