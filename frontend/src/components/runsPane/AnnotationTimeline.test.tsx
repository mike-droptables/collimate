// An annotation pane timeline: messages and file touches in time order,
// the touches' rows (an Open for a file that still is one), the phase line
// and queue after them, the empty text, the tail it follows, and the
// composer it places: the add row at its end, the composer there or in
// place of the message it edits, the keyboard in and out, the pool woken as
// it scrolls under an open composer.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { createRef, forwardRef, useImperativeHandle } from 'react';

const reveal = vi.hoisted(() => vi.fn());
vi.mock('@/lib/revealFile', () => ({ revealCellFile: reveal }));
const pool = vi.hoisted(() => ({ invalidate: vi.fn() }));
vi.mock('@/contexts/GuestPoolContext', async (importOriginal) => {
    const actual = await importOriginal<typeof import('@/contexts/GuestPoolContext')>();
    return { ...actual, useGuestPool: () => ({ ...actual.useGuestPool(), invalidate: pool.invalidate, containerOf: () => null }) };
});
vi.mock('./DraftEditor', () => ({
    DraftEditor: forwardRef(function FakeEditor(_p: unknown, ref) {
        useImperativeHandle(ref, () => ({ focus: () => { (document.querySelector('[data-testid="fake-editor"]') as HTMLElement | null)?.focus(); return true; }, text: async () => '', reset: () => {} }));
        return <textarea data-testid="fake-editor" />;
    }),
}));

import { AnnotationTimeline, type AnnotationTimelineHandle, type AnnotationTimelineProps, type TimelineComposer } from './AnnotationTimeline';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { buildTimeline, touchesFromCommit } from '@/lib/annotations';
import { CELL_ID, ctx, msg, seedPaneStore, touch } from '@/test/annotationFixtures';

const items = buildTimeline(
    [{ thread: { root: msg('m1', { author: 'run', read_at: 'x', created_at: '2026-10-24 10:00:03' }), replies: [] }, contextId: 'run-1', tag: 'agent', sendable: false }],
    [
        { touch: touch('a.md', '2026-10-24 10:00:01', { op: 'create' }), tag: 'agent' },
        { touch: touch('b.md', '2026-10-24 10:00:02'), tag: null },
        { touch: touch('gone.md', '2026-10-24 10:00:04', { op: 'delete' }), tag: null },
        { touch: touch('dir', '2026-10-24 10:00:05', { op: 'create', folder: true }), tag: null },
    ],
);
const mount = (props: Partial<AnnotationTimelineProps> = {}) => render(
    <AnnotationTimeline cellId={CELL_ID} channel="ch" items={items} readOnly={false} draftKey="k" emptyText="No messages yet." readerOf={() => 'Agent'} {...props} />,
);

beforeEach(() => { reveal.mockReset(); pool.invalidate.mockClear(); seedPaneStore({ openConfirm: vi.fn(async () => true) }); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('AnnotationTimeline', () => {
    it('interleaves touches and messages by time; each touch row says what happened, with its tag', () => {
        mount();
        const log = screen.getByTestId('annotation-timeline');
        expect(log).toHaveAttribute('role', 'log');
        const order = [...log.querySelectorAll('[data-testid="file-touch-row"], [data-testid="annotation-message"]')]
            .map(el => el.getAttribute('data-path') ?? el.getAttribute('data-id'));
        expect(order).toEqual(['a.md', 'b.md', 'm1', 'gone.md', 'dir']);
        const rows = screen.getAllByTestId('file-touch-row');
        expect(rows.map(r => [r.getAttribute('data-op'), r.getAttribute('data-folder')])).toEqual([['create', 'false'], ['edit', 'false'], ['delete', 'false'], ['create', 'true']]);
        expect(rows[0].textContent).toMatch(/^Createda\.mdagent\d\d:\d\d$/);
        expect(rows[1].textContent).toContain('Edited');
        expect(rows[2].textContent).toContain('Deleted');
        expect(rows[0].querySelector('.lucide-file-plus')).not.toBeNull();
        expect(rows[3].querySelector('.lucide-folder-plus')).not.toBeNull();
    });

    it('an edit that replaced a newer save deliberately says whose revision it replaced', () => {
        const [replaced] = touchesFromCommit({ cell_id: 'v9', changes: [{ path: 'notes.md', op: 'edit', folder: false,
            replaced: { revision: 3, writer_id: 'browser', writer: { kind: 'user' } } }] }, '2026-10-24 10:00:06');
        mount({ items: buildTimeline([], [{ touch: replaced, tag: null }, { touch: touch('b.md', '2026-10-24 10:00:07'), tag: null }]) });
        const [row, plain] = screen.getAllByTestId('file-touch-row');
        expect(row.querySelector('[data-testid="file-touch-replaced"]')!.textContent).toBe('replaced your revision 3 deliberately');
        expect(plain.querySelector('[data-testid="file-touch-replaced"]')).toBeNull();
    });

    it('a touched file opens in a new tab; a deleted file or a folder does not', () => {
        mount();
        const [created, , deleted, folder] = screen.getAllByTestId('file-touch-row');
        fireEvent.click(created.querySelector('button')!);
        expect(reveal).toHaveBeenCalledWith(CELL_ID, 'a.md', { newTab: true });
        expect(deleted.querySelector('button')).toBeNull();
        expect(folder.querySelector('button')).toBeNull();
    });

    it('shows its empty text with nothing in it, and the phase line and queue after the history', () => {
        mount({ items: [] });
        expect(screen.getByText('No messages yet.')).toBeInTheDocument();
        cleanup();
        mount({ items: [], phase: <div data-testid="phase" />, queue: { contextId: 'run-1', unheld: [{ root: msg('q1'), replies: [] }], held: [], barOn: false } });
        expect(screen.queryByText('No messages yet.')).toBeNull();
        const log = screen.getByTestId('annotation-timeline');
        expect(log.lastElementChild).toBe(screen.getByTestId('queue-section'));
        expect(screen.getByTestId('phase').nextElementSibling).toBe(screen.getByTestId('queue-section'));
        expect(screen.getByTestId('message-drag')).toBeInTheDocument();
    });

    it('follows its tail while at the bottom, and stays put once the user has scrolled up', () => {
        let height = 500;
        vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(() => height);
        vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(100);
        const { rerender } = mount({ items: items.slice(0, 2) });
        const log = screen.getByTestId('annotation-timeline');
        expect(log.scrollTop).toBe(500);
        height = 700;
        rerender(<AnnotationTimeline cellId={CELL_ID} channel="ch" items={items.slice(0, 3)} readOnly={false} draftKey="k" emptyText="" readerOf={() => 'Agent'} />);
        expect(log.scrollTop).toBe(700);
        log.scrollTop = 100;
        fireEvent.scroll(log);
        height = 900;
        rerender(<AnnotationTimeline cellId={CELL_ID} channel="ch" items={items} readOnly={false} draftKey="k" emptyText="" readerOf={() => 'Agent'} />);
        expect(log.scrollTop).toBe(100);
    });

    it('follows a reply landing in a thread already shown, in the history or the queue, though no item is added', () => {
        let height = 500;
        vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(() => height);
        vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(100);
        const root = msg('m1', { read_at: '2026-10-24 10:00:01' });
        const timeline = (replies: ReturnType<typeof msg>[], queued: ReturnType<typeof msg>[] = []) => (
            <AnnotationTimeline cellId={CELL_ID} channel="ch" readOnly={false} draftKey="k" emptyText="" readerOf={() => 'Agent'}
                items={buildTimeline([{ thread: { root, replies }, contextId: 'run-1', tag: null, sendable: false }], [])}
                queue={{ contextId: 'run-1', unheld: [{ root: msg('q1'), replies: queued }], held: [], barOn: false }} />
        );
        const { rerender } = render(timeline([]));
        const log = screen.getByTestId('annotation-timeline');
        expect(log.scrollTop).toBe(500);
        height = 700;
        rerender(timeline([msg('r1', { author: 'run', parent_id: 'm1', read_at: 'x', created_at: '2026-10-24 10:00:02' })]));
        expect(log.scrollTop).toBe(700);
        // Another reply in place of that one: the count holds, the tail still moves.
        height = 750;
        rerender(timeline([msg('r2', { author: 'run', parent_id: 'm1', read_at: 'x', created_at: '2026-10-24 10:00:02' })]));
        expect(log.scrollTop).toBe(750);
        height = 800;
        rerender(timeline([msg('r2', { author: 'run', parent_id: 'm1', read_at: 'x', created_at: '2026-10-24 10:00:02' })], [msg('q1r', { parent_id: 'q1' })]));
        expect(log.scrollTop).toBe(800);
    });
});

const KEY = `${CELL_ID}:run:run-1`;
const COMPOSER: TimelineComposer = {
    scope: { kind: 'run', runId: 'run-1' }, contextId: 'run-1', context: ctx('run-1'), barOn: false, stopping: false, maximized: false, addLabel: 'Add message',
};
const draft = () => useCollimateStore.getState().composerDrafts[KEY];
/** A run's timeline with a history message (read) and two queued ones. */
const withComposer = (props: Partial<AnnotationTimelineProps> = {}) => {
    const ref = createRef<AnnotationTimelineHandle>();
    const view = render(
        <AnnotationTimeline ref={ref} cellId={CELL_ID} channel="ch" readOnly={false} draftKey={KEY} emptyText="" readerOf={() => 'Agent'}
            items={buildTimeline([{ thread: { root: msg('h1', { read_at: 'x' }), replies: [] }, contextId: 'run-1', tag: null, sendable: false }], [])}
            queue={{ contextId: 'run-1', unheld: [{ root: msg('q1'), replies: [] }, { root: msg('q2'), replies: [msg('q2r', { parent_id: 'q2' })] }], held: [], barOn: false }}
            composer={COMPOSER} {...props} />,
    );
    return { ref, ...view };
};
const message = (id: string) => screen.getAllByTestId('annotation-message').find(m => m.dataset.id === id)!;

describe('AnnotationTimeline — its composer', () => {
    it('ends with the add row; it opens the composer in its place, holding the keys', () => {
        withComposer();
        const log = screen.getByTestId('annotation-timeline');
        const add = screen.getByTestId('composer-add');
        expect(log.lastElementChild).toBe(add);
        expect(add.textContent).toBe('Add message');
        expect(add.querySelector('.lucide-plus')).not.toBeNull();
        expect(screen.queryByTestId('annotation-composer')).toBeNull();
        fireEvent.click(add);
        expect(draft().open).toBe(true);
        expect(log.lastElementChild).toBe(screen.getByTestId('annotation-composer'));
        expect(screen.queryByTestId('composer-add')).toBeNull();
        expect(document.activeElement).toBe(screen.getByTestId('fake-editor'));
    });

    it('read-only, or with no composer, there is no add row', () => {
        withComposer({ readOnly: true });
        expect(screen.queryByTestId('composer-add')).toBeNull();
        act(() => useCollimateStore.getState().setComposerDraft(KEY, { open: true }));
        expect(screen.queryByTestId('annotation-composer')).toBeNull();
        cleanup();
        withComposer({ composer: undefined });
        expect(screen.queryByTestId('composer-add')).toBeNull();
    });

    it('a draft open as the timeline mounts shows its composer but takes no keys', () => {
        act(() => useCollimateStore.getState().setComposerDraft(KEY, { open: true }));
        withComposer();
        expect(screen.getByTestId('annotation-composer')).toBeInTheDocument();
        expect(document.activeElement).toBe(document.body);
    });

    it('Esc in the inline composer closes it to the add row, which takes the keys; the draft text stays', () => {
        withComposer();
        fireEvent.click(screen.getByTestId('composer-add'));
        act(() => useCollimateStore.getState().setComposerDraft(KEY, { text: 'kept' }));
        fireEvent.keyDown(screen.getByTestId('fake-editor'), { key: 'Escape' });
        expect(screen.queryByTestId('annotation-composer')).toBeNull();
        expect(document.activeElement).toBe(screen.getByTestId('composer-add'));
        expect(draft()).toMatchObject({ open: false, text: 'kept' });
    });

    it('Esc on an in-place edit abandons the edit: the add row then writes a new message', () => {
        withComposer();
        fireEvent.click(within(message('q1')).getByTestId('message-edit'));
        expect(message('q1')).toHaveAttribute('data-editing', 'true');
        fireEvent.keyDown(screen.getByTestId('fake-editor'), { key: 'Escape' });
        expect(screen.queryByTestId('annotation-composer')).toBeNull();
        expect(document.activeElement).toBe(screen.getByTestId('composer-add'));
        expect(draft()).toMatchObject({ open: false, editing: null, text: '' });
        fireEvent.click(screen.getByTestId('composer-add'));
        expect(message('q1')).not.toHaveAttribute('data-editing');
        expect(screen.queryByTestId('composer-editing')).toBeNull();
        expect(screen.getByTestId('annotation-timeline').lastElementChild).toBe(screen.getByTestId('annotation-composer'));
    });

    it('an edit swaps the message (or a reply) in place for the composer; one not shown edits at the end', () => {
        withComposer();
        fireEvent.click(within(message('q1')).getByTestId('message-edit'));
        expect(message('q1')).toHaveAttribute('data-editing', 'true');
        expect(within(message('q1')).getByTestId('composer-editing')).toBeInTheDocument();
        expect(document.activeElement).toBe(screen.getByTestId('fake-editor'));
        // Another edit moves it there.
        act(() => useCollimateStore.getState().loadDraftForEdit(KEY, msg('q2r', { parent_id: 'q2' })));
        expect(message('q1')).not.toHaveAttribute('data-editing');
        expect(message('q2r')).toHaveAttribute('data-editing', 'true');
        expect(message('q2r')).toHaveAttribute('data-reply', 'true');
        expect(message('q2').textContent).toContain('body of q2');
        expect(screen.getAllByTestId('annotation-composer')).toHaveLength(1);
        act(() => useCollimateStore.getState().loadDraftForEdit(KEY, msg('elsewhere')));
        expect(screen.getByTestId('annotation-timeline').lastElementChild).toBe(screen.getByTestId('annotation-composer'));
    });

    it('a reply opens the composer at the end with its banner and puts the keys there', () => {
        withComposer();
        fireEvent.click(within(message('h1')).getByTestId('message-reply'));
        expect(screen.getByTestId('annotation-timeline').lastElementChild).toBe(screen.getByTestId('annotation-composer'));
        expect(screen.getByTestId('composer-reply-to')).toBeInTheDocument();
        expect(document.activeElement).toBe(screen.getByTestId('fake-editor'));
    });

    it('focusComposer() opens a closed composer and focuses it, or focuses the open one', () => {
        const { ref } = withComposer();
        act(() => ref.current!.focusComposer());
        expect(draft().open).toBe(true);
        expect(document.activeElement).toBe(screen.getByTestId('fake-editor'));
        act(() => (document.activeElement as HTMLElement).blur());
        act(() => ref.current!.focusComposer());
        expect(document.activeElement).toBe(screen.getByTestId('fake-editor'));
    });

    it('opening scrolls the log just enough to show the composer', () => {
        withComposer();
        const log = screen.getByTestId('annotation-timeline');
        vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
            const box = this.dataset.testid === 'annotation-timeline' ? { top: 0, bottom: 100 } : { top: 140, bottom: 200 };
            return { ...box, left: 0, right: 100, width: 100, height: box.bottom - box.top, x: 0, y: box.top, toJSON: () => ({}) } as DOMRect;
        });
        log.scrollTop = 0;
        fireEvent.click(screen.getByTestId('composer-add'));
        expect(log.scrollTop).toBe(100);
    });

    it('a scroll wakes the guest pool while the composer is open (its editor may be a pooled frame), not before', () => {
        withComposer();
        const log = screen.getByTestId('annotation-timeline');
        pool.invalidate.mockClear();
        fireEvent.scroll(log);
        expect(pool.invalidate).not.toHaveBeenCalled();
        fireEvent.click(screen.getByTestId('composer-add'));
        pool.invalidate.mockClear();
        fireEvent.scroll(log);
        expect(pool.invalidate).toHaveBeenCalledTimes(1);
    });
});
