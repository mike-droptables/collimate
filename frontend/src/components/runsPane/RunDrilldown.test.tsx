// One run's drilldown: back, name and Stop on the rail (a finished run's
// status chip instead); Default | Current file | Log; the timeline of its
// messages and touches with its phase line and queue; the not-listening
// banner; the approval cards from its inbox; the composer (never for an
// ended run); Esc back.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { createRef, forwardRef, useImperativeHandle } from 'react';

const inbox = vi.hoisted(() => ({ cards: [] as unknown[], answer: vi.fn(() => true), calls: [] as unknown[][] }));
vi.mock('@/hooks/useRunInbox', () => ({ useRunInbox: (...args: unknown[]) => { inbox.calls.push(args); return { cards: inbox.cards, status: 'open', answer: inbox.answer }; } }));
vi.mock('./DraftEditor', () => ({
    DraftEditor: forwardRef(function FakeEditor(_p: unknown, ref) {
        useImperativeHandle(ref, () => ({ focus: () => true, text: async () => '', reset: () => {} }));
        return <textarea data-testid="fake-editor" />;
    }),
}));

import { RunDrilldown, type RunViewHandle } from './RunDrilldown';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { CELL_ID, RUN, ctx, finishedRun, msg, seedPaneStore, touch } from '@/test/annotationFixtures';
import type { RunsPaneOption } from '@/stores/collimate/types';

const store = () => useCollimateStore.getState();
const mount = (option: RunsPaneOption = 'default', props: Partial<Parameters<typeof RunDrilldown>[0]> = {}) => {
    const onBack = vi.fn();
    const ref = createRef<RunViewHandle>();
    const view = render(<RunDrilldown ref={ref} cellId={CELL_ID} runId={RUN.runId} option={option} allowAnnotate maximized={false} onBack={onBack} {...props} />);
    return { onBack, ref, ...view };
};
const order = () => [...screen.getByTestId('annotation-timeline').querySelectorAll('[data-testid="file-touch-row"], [data-testid="annotation-message"], [data-testid="drilldown-phase"], [data-testid="queue-bar-line"]')]
    .map(el => el.getAttribute('data-path') ?? el.getAttribute('data-id') ?? el.getAttribute('data-testid'));

const CONTEXT = ctx(RUN.runId, [
    msg('prompt', { kind: 'prompt', read_at: '2026-10-24 09:59:00', created_at: '2026-10-24 09:59:00', position: 0 }),
    msg('read', { read_at: '2026-10-24 10:03:00', created_at: '2026-10-24 10:00:00', position: 1, context_items: [{ path: 'page.html', renderer_id: 'html', label: 'page.html › #x', text: 'x', anchor: null }] }),
    msg('reply', { author: 'run', author_label: RUN.label, read_at: '2026-10-24 10:04:00', created_at: '2026-10-24 10:04:00', parent_id: 'read' }),
    msg('q1', { position: 3 }), msg('h1', { position: 5, held: true, context_items: [{ path: 'page.html', renderer_id: 'html', label: 'l', text: 't', anchor: null }] }),
], { bar_on: true });

beforeEach(() => {
    inbox.cards = [];
    inbox.answer.mockClear();
    inbox.calls = [];
    seedPaneStore({
        runs: { [RUN.runId]: RUN }, annotationContexts: { [CELL_ID]: { [RUN.runId]: CONTEXT } },
        fileTouches: { [RUN.runId]: [touch('page.html', '2026-10-24 10:02:00'), touch('other.md', '2026-10-24 10:05:00')] },
        lastFileTab: { [CELL_ID]: 'page.html' }, sendDraft: vi.fn(async () => true),
    });
});
afterEach(cleanup);

describe('RunDrilldown', () => {
    it('says on its phase line that the run adopted its harness\'s minimum grant', () => {
        const grant = { docker: 'full', internet: 'full', commands: 'deny' };
        useCollimateStore.setState({ activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [
            { run_id: RUN.runId, cell_id: CELL_ID, adopted: true, effective_grant: grant, harness: 'codex', harness_label: 'Codex' },
        ] }] } as never);
        mount();
        expect(within(screen.getByTestId('drilldown-phase')).getByTestId('drilldown-adopted').textContent)
            .toBe('Docker full adopted for Codex');
        cleanup();
        useCollimateStore.setState({ activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [
            { run_id: RUN.runId, cell_id: CELL_ID, adopted: false, effective_grant: grant, harness: 'codex', harness_label: 'Codex' },
        ] }] } as never);
        mount();
        expect(screen.queryByTestId('drilldown-adopted')).toBeNull();
    });

    it('says on its phase line that the run\'s harness CLI runs on this machine', () => {
        const grant = { docker: 'none', internet: 'full', commands: 'ask' };
        useCollimateStore.setState({ activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [
            { run_id: RUN.runId, cell_id: CELL_ID, adopted: false, effective_grant: grant, harness: 'antigravity', harness_label: 'Antigravity', cli_locus: 'host' },
        ] }] } as never);
        mount();
        expect(within(screen.getByTestId('drilldown-phase')).getByTestId('drilldown-host-cli').textContent)
            .toBe('Antigravity CLI on this machine');
        expect(screen.queryByTestId('drilldown-adopted')).toBeNull();
        cleanup();
        useCollimateStore.setState({ activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [
            { run_id: RUN.runId, cell_id: CELL_ID, adopted: false, effective_grant: grant, harness: 'antigravity', harness_label: 'Antigravity', cli_locus: 'container' },
        ] }] } as never);
        mount();
        expect(screen.queryByTestId('drilldown-host-cli')).toBeNull();
    });

    it('heads the rail with back, the run\'s name and its Stop; back goes back', () => {
        const { onBack } = mount();
        const root = screen.getByTestId('run-drilldown');
        expect(root).toHaveAttribute('data-run-id', RUN.runId);
        expect(root).not.toHaveAttribute('data-ended');
        expect(root.textContent).toContain('Agent 11111111');
        // A narrow rail: Stop reads its verb alone, never shrinks, and the title truncates instead.
        const stop = within(root).getByTestId('run-stop');
        expect(stop).toHaveAccessibleName('Stop ‘Agent’');
        expect(stop.textContent).toBe('Stop');
        expect(stop.querySelector('kbd')).toBeNull();
        expect(stop.className).toContain('flex-none');
        const title = within(stop.parentElement!).getByText('Agent 11111111');
        expect(title.className).toContain('truncate');
        expect(title.parentElement!.className).toContain('min-w-0');
        fireEvent.click(screen.getByTestId('drilldown-back'));
        expect(onBack).toHaveBeenCalled();
        expect(store().refreshAnnotationContext).toHaveBeenCalledWith(CELL_ID, RUN.runId);
        expect(store().loadFileTouches).not.toHaveBeenCalled();
        expect(inbox.calls[0]).toEqual(['ch', RUN.runId, true]);
    });

    it('Default: history and touches in time order, then the phase line, the queue, the bar and the held', () => {
        mount();
        expect(order()).toEqual(['prompt', 'page.html', 'read', 'reply', 'other.md', 'drilldown-phase', 'q1', 'queue-bar-line', 'h1']);
        expect(screen.getByTestId('drilldown-phase')).toHaveAttribute('data-phase', 'working');
        expect(screen.getByTestId('drilldown-phase').textContent).toBe('Working');
        expect(screen.getByTestId('drilldown-option-default')).toHaveAttribute('aria-selected', 'true');
        expect(screen.getAllByTestId('message-drag')).toHaveLength(2);
    });

    it('the options switch the view and read Default | Current file | Log', () => {
        mount();
        const tabs = screen.getAllByRole('tab');
        expect(tabs.map(t => t.textContent)).toEqual(['Default', 'Current file', 'Log']);
        expect(screen.getByTestId('drilldown-option-file')).toHaveAttribute('title', 'page.html');
        fireEvent.click(screen.getByTestId('drilldown-option-log'));
        expect(store().runsPaneView[CELL_ID]).toEqual({ view: 'run', runId: RUN.runId, option: 'log' });
    });

    it('Current file keeps what is about the file last viewed; with none open it says so', () => {
        mount('file');
        expect(screen.getByTestId('drilldown-file-head').textContent).toBe('page.html');
        expect(order()).toEqual(['page.html', 'read', 'reply', 'drilldown-phase', 'queue-bar-line', 'h1']);
        cleanup();
        useCollimateStore.setState({ lastFileTab: {} });
        mount('file');
        expect(screen.getByTestId('drilldown-file-head').textContent).toBe('No file is open in this cell.');
        expect(order()).toEqual(['drilldown-phase', 'queue-bar-line']);
    });

    it('Current file moves a message among those shown, sending the whole queue in its new order', () => {
        const reorderContextQueue = vi.fn(async () => {});
        useCollimateStore.setState({ reorderContextQueue });
        mount('file');
        expect(screen.getAllByTestId('message-drag')).toHaveLength(1);
        fireEvent.keyDown(screen.getByTestId('message-drag'), { key: 'ArrowUp' });
        // h1 moves above the bar; q1, about another file, keeps its place first.
        expect(reorderContextQueue).toHaveBeenCalledWith(CELL_ID, RUN.runId, ['q1', 'h1'], 2);
    });

    it('Log shows the run\'s own lines', () => {
        useCollimateStore.setState({
            cellLogRuns: { [CELL_ID]: [{ runId: RUN.runId, channel: 'ch', runner: 'Agent', startedAt: 1 }, { runId: 'x', channel: 'ch', runner: 'X', startedAt: 1 }] },
            runLogs: { [RUN.runId]: ['[10:00:00] INFO   mine'], x: ['[10:00:00] INFO   theirs'] },
        });
        mount('log');
        expect(screen.getByTestId('run-log').textContent).toContain('INFO mine');
        expect(screen.getByTestId('run-log').textContent).not.toContain('theirs');
    });

    it('a run not reading its messages yet says so, live only', () => {
        useCollimateStore.setState({ annotationContexts: { [CELL_ID]: { [RUN.runId]: { ...CONTEXT, listening: false } } } });
        mount();
        expect(screen.getByTestId('annotation-not-listening').textContent).toContain('This run isn\'t reading annotations yet.');
    });

    it('the phase line follows the run: starting, waiting on the user, stopping', () => {
        useCollimateStore.setState({ runPhases: { [RUN.runId]: { phase: 'awaiting_approval' } } });
        mount();
        const phase = screen.getByTestId('drilldown-phase');
        expect(phase).toHaveAttribute('data-phase', 'waiting');
        expect(phase.textContent).toBe('Waiting for your approval');
        expect(phase.querySelector('i')!.className).toContain('bg-brass');
        act(() => useCollimateStore.setState({ runPhases: { [RUN.runId]: { phase: 'spinning_up' } } }));
        expect(phase.textContent).toBe('Starting');
        act(() => useCollimateStore.setState({ stoppingRunIds: new Set([RUN.runId]) }));
        expect(phase).toHaveAttribute('data-phase', 'stopping');
        expect(screen.getByTestId('composer-notice').textContent).toBe('This run is stopping.');
    });

    it('shows the run\'s approval cards, answered through its inbox', () => {
        inbox.cards = [{ id: 'p1', title: 'Run a command', kind: 'execute', options: [{ option_id: 'ok', name: 'Allow', kind: 'allow_once' }] }];
        mount('log');
        fireEvent.click(screen.getByTestId('approval-option'));
        expect(inbox.answer).toHaveBeenCalledWith('p1', 'ok');
        fireEvent.click(screen.getByTestId('approval-cancel'));
        expect(inbox.answer).toHaveBeenCalledWith('p1', null);
    });

    it('a finished run is read-only: its status chip, no Stop, no composer, no actions; seeing it marks it seen', () => {
        useCollimateStore.setState({ runs: {}, finishedRuns: { [CELL_ID]: [finishedRun(RUN, { status: 'stopped' })] } });
        mount();
        const root = screen.getByTestId('run-drilldown');
        expect(root).toHaveAttribute('data-ended', 'true');
        expect(within(root).queryByTestId('run-stop')).toBeNull();
        expect(root.textContent).toContain('Stopped');
        expect(screen.queryByTestId('annotation-composer')).toBeNull();
        expect(screen.queryByTestId('message-edit')).toBeNull();
        expect(screen.queryByTestId('message-drag')).toBeNull();
        expect(screen.getByTestId('drilldown-phase')).toHaveAttribute('data-phase', 'ended');
        expect(screen.getByTestId('drilldown-phase').textContent).toBe('Ended · Stopped');
        expect(store().markFailureSeen).toHaveBeenCalledWith(CELL_ID);
        expect(inbox.calls.at(-1)).toEqual(['ch', RUN.runId, false]);
    });

    it('a sealed context or a read-only cell shows no composer; an unknown run renders nothing', () => {
        useCollimateStore.setState({ annotationContexts: { [CELL_ID]: { [RUN.runId]: { ...CONTEXT, sealed: true } } } });
        mount();
        expect(screen.queryByTestId('annotation-composer')).toBeNull();
        cleanup();
        useCollimateStore.setState({ annotationContexts: { [CELL_ID]: { [RUN.runId]: CONTEXT } } });
        mount('default', { allowAnnotate: false });
        expect(screen.queryByTestId('annotation-composer')).toBeNull();
        cleanup();
        const { container } = mount('default', { runId: 'gone' });
        expect(container.innerHTML).toBe('');
    });

    it('loads the run\'s touches once when it has none; Esc anywhere goes back; an expanded composer hides the timeline', () => {
        useCollimateStore.setState({ fileTouches: {} });
        const { onBack } = mount();
        expect(store().loadFileTouches).toHaveBeenCalledWith('ch', RUN.runId);
        fireEvent.keyDown(screen.getByTestId('annotation-timeline'), { key: 'Escape' });
        expect(onBack).toHaveBeenCalledTimes(1);
        fireEvent.click(screen.getByTestId('composer-expand'));
        expect(screen.getByRole('tabpanel').className).toContain('hidden');
    });

    it('focus() takes the timeline, or the composer; a notice shows under the options', () => {
        const { ref } = mount();
        act(() => ref.current!.focus('view'));
        expect(document.activeElement).toBe(screen.getByTestId('annotation-timeline'));
        act(() => ref.current!.focus('composer'));
        act(() => useCollimateStore.setState({ annotationNotice: { [CELL_ID]: 'That run is no longer live.' } }));
        expect(screen.getByTestId('annotation-notice').textContent).toBe('That run is no longer live.');
        // The composer's Esc with nothing to collapse hands the keys back to the timeline.
        act(() => screen.getByTestId('fake-editor').focus());
        fireEvent.keyDown(screen.getByTestId('fake-editor'), { key: 'Escape' });
        expect(document.activeElement).toBe(screen.getByTestId('annotation-timeline'));
    });

    it('a click on the timeline right after a Stop is not swallowed (the stop lock is the rail\'s)', () => {
        mount();
        fireEvent.click(screen.getByTestId('run-stop'));
        fireEvent.click(screen.getAllByTestId('message-reply')[0]);
        expect(store().composerDrafts[`${CELL_ID}:run:${RUN.runId}`].replyTo).not.toBeNull();
    });
});
