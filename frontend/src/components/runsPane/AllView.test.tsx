// All: every live run's messages (tagged with the run's label) and the
// cell's own notes (untagged; each sendable to a live run), with every live
// run's touches, in time order; Current file; the merged cell log; a
// composer writing to the cell's own context; back and Esc.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { createRef, forwardRef, useImperativeHandle } from 'react';

vi.mock('./DraftEditor', () => ({
    DraftEditor: forwardRef(function FakeEditor(_p: unknown, ref) {
        useImperativeHandle(ref, () => ({ focus: () => { (document.querySelector('[data-testid="fake-editor"]') as HTMLElement).focus(); return true; }, text: async () => 'a note', reset: () => {} }));
        return <textarea data-testid="fake-editor" />;
    }),
}));

import { AllView } from './AllView';
import type { RunViewHandle } from './RunDrilldown';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { CELL_ID, RUN, RUN_2, ctx, msg, seedPaneStore, touch } from '@/test/annotationFixtures';
import type { RunsPaneOption } from '@/stores/collimate/types';

const store = () => useCollimateStore.getState();
const mount = (option: RunsPaneOption = 'default', allowAnnotate = true) => {
    const onBack = vi.fn();
    const ref = createRef<RunViewHandle>();
    render(<AllView ref={ref} cellId={CELL_ID} option={option} allowAnnotate={allowAnnotate} maximized={false} onBack={onBack} />);
    return { onBack, ref };
};
const ids = () => [...screen.getByTestId('annotation-timeline').querySelectorAll('[data-testid="file-touch-row"], [data-testid="annotation-message"]')]
    .map(el => el.getAttribute('data-path') ?? el.getAttribute('data-id'));

beforeEach(() => {
    seedPaneStore({
        runs: { [RUN.runId]: RUN },
        annotationContexts: { [CELL_ID]: {
            [RUN.runId]: ctx(RUN.runId, [msg('r1', { created_at: '2026-10-24 10:02:00', context_items: [{ path: 'page.html', renderer_id: 'html', label: 'l', text: 't', anchor: null }] })]),
            [RUN_2.runId]: ctx(RUN_2.runId, [msg('ended-run', { context_id: RUN_2.runId })]),
            cell: ctx('cell', [msg('n1', { context_id: 'cell', created_at: '2026-10-24 10:01:00' })]),
        } },
        fileTouches: { [RUN.runId]: [touch('page.html', '2026-10-24 10:03:00')] },
        lastFileTab: { [CELL_ID]: 'page.html' },
        sendDraft: vi.fn(async () => true), sendMessageToRun: vi.fn(async () => {}), openConfirm: vi.fn(async () => true),
    });
});
afterEach(cleanup);

describe('AllView', () => {
    it('interleaves the live runs\' messages (tagged), the cell\'s notes (untagged) and the runs\' touches', () => {
        mount();
        expect(ids()).toEqual(['n1', 'r1', 'page.html']);
        const [note, runMessage] = screen.getAllByTestId('annotation-message');
        expect(within(runMessage).getByTestId('message-tag').textContent).toBe(RUN.label);
        expect(within(note).queryByTestId('message-tag')).toBeNull();
        expect(within(note).getByTestId('message-send-to')).toBeInTheDocument();
        expect(within(runMessage).queryByTestId('message-send-to')).toBeNull();
        expect(screen.getByTestId('file-touch-row').textContent).toContain(RUN.label);
        expect(store().loadCellAnnotations).toHaveBeenCalledWith(CELL_ID);
    });

    it('sends a cell note to a live run picked from its menu; with no live run the button is inert', async () => {
        mount();
        fireEvent.click(screen.getByTestId('message-send-to'));
        fireEvent.click(within(screen.getByRole('menu', { name: 'Send to run' })).getByText('Agent 11111111'));
        await vi.waitFor(() => expect(store().sendMessageToRun).toHaveBeenCalledWith(CELL_ID, 'n1', RUN.runId));
        act(() => useCollimateStore.setState({ runs: {} }));
        expect(screen.getByTestId('message-send-to')).toHaveAttribute('aria-disabled', 'true');
    });

    it('Current file keeps what is about the file; with none open, nothing', () => {
        mount('file');
        expect(ids()).toEqual(['r1', 'page.html']);
        expect(screen.getByTestId('drilldown-file-head').textContent).toBe('page.html');
        cleanup();
        useCollimateStore.setState({ lastFileTab: {} });
        mount('file');
        expect(ids()).toEqual([]);
        expect(screen.getByText('Nothing about this file yet.')).toBeInTheDocument();
    });

    it('Log is the merged cell log, with runners', () => {
        useCollimateStore.setState({
            cellLogRuns: { [CELL_ID]: [{ runId: 'a', channel: 'ch', runner: 'Agent', startedAt: 1 }, { runId: 'b', channel: 'ch', runner: 'Chat', startedAt: 1 }] },
            runLogs: { a: ['[10:00:00] INFO   one'], b: ['[10:01:00] INFO   two'] },
        });
        mount('log');
        expect(screen.getByTestId('run-log').textContent).toContain('Agent  INFO one');
        expect(screen.getByTestId('run-log').textContent).toContain('Chat  INFO two');
    });

    it('its composer writes to the cell\'s own context and offers no queue bar', async () => {
        mount();
        expect(screen.queryByTestId('queue-bar')).toBeNull();
        expect(screen.queryByTestId('composer-place')).toBeNull();
        fireEvent.click(screen.getByTestId('composer-send'));
        await vi.waitFor(() => expect(store().sendDraft).toHaveBeenCalledWith(CELL_ID, `${CELL_ID}:all`, 'cell', 'a note'));
    });

    it('loads the touches of a live run that has none yet', () => {
        useCollimateStore.setState({ fileTouches: {} });
        mount();
        expect(store().loadFileTouches).toHaveBeenCalledWith('ch', RUN.runId);
    });

    it('back and Esc go back; read-only has no composer; focus() takes the timeline or the composer', () => {
        const { onBack, ref } = mount();
        fireEvent.click(screen.getByTestId('all-back'));
        fireEvent.keyDown(screen.getByTestId('annotation-timeline'), { key: 'Escape' });
        expect(onBack).toHaveBeenCalledTimes(2);
        act(() => ref.current!.focus('view'));
        expect(document.activeElement).toBe(screen.getByTestId('annotation-timeline'));
        act(() => ref.current!.focus('composer'));
        expect(document.activeElement).toBe(screen.getByTestId('fake-editor'));
        fireEvent.keyDown(screen.getByTestId('fake-editor'), { key: 'Escape' });
        expect(document.activeElement).toBe(screen.getByTestId('annotation-timeline'));
        act(() => useCollimateStore.setState({ annotationNotice: { [CELL_ID]: 'x' } }));
        expect(screen.getByTestId('annotation-notice').textContent).toBe('x');
        fireEvent.click(screen.getByTestId('composer-expand'));
        expect(screen.getByRole('tabpanel').className).toContain('hidden');
        cleanup();
        mount('default', false);
        expect(screen.queryByTestId('annotation-composer')).toBeNull();
        expect(screen.queryByTestId('message-edit')).toBeNull();
    });
});
