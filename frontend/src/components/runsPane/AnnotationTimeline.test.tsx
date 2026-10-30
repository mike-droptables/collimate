// A status pane timeline: messages and file touches in time order, the
// touches' rows (an Open for a file that still is one), the phase line and
// queue after them, the empty text, and the tail it follows.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

const reveal = vi.hoisted(() => vi.fn());
vi.mock('@/lib/revealFile', () => ({ revealCellFile: reveal }));

import { AnnotationTimeline, type AnnotationTimelineProps } from './AnnotationTimeline';
import { buildTimeline } from '@/lib/annotations';
import { CELL_ID, msg, seedPaneStore, touch } from '@/test/annotationFixtures';

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

beforeEach(() => { reveal.mockReset(); seedPaneStore(); });
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
