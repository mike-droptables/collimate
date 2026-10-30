// The status view: the cell's live runs (state, name and short id, whether
// each reads its messages, elapsed, its UI file's Open), the runs that ended
// this session below them until Clear finished, the footer's All and New
// run, and its keys.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { createRef } from 'react';

const reveal = vi.hoisted(() => vi.fn());
vi.mock('@/lib/revealFile', () => ({ revealCellFile: reveal }));

import { StatusView, type StatusViewHandle } from './StatusView';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { CELL_ID, RUN, RUN_2, ctx, finishedRun, seedPaneStore } from '@/test/annotationFixtures';

const store = () => useCollimateStore.getState();
const rows = () => screen.getAllByTestId('status-run-row');
const mount = (props: Partial<Parameters<typeof StatusView>[0]> = {}) => {
    const onClose = vi.fn();
    const ref = createRef<StatusViewHandle>();
    render(<StatusView ref={ref} cellId={CELL_ID} allowRun focusRunId={null} onClose={onClose} {...props} />);
    return { onClose, ref };
};

beforeEach(() => {
    reveal.mockReset();
    seedPaneStore({ openPickSession: vi.fn() });
});
afterEach(cleanup);

describe('StatusView', () => {
    it('chips a run whose grant was raised to its harness\'s minimum, and says so in its title', () => {
        const grant = { docker: 'full', internet: 'full', commands: 'deny' };
        seedPaneStore({
            runs: { [RUN.runId]: RUN, [RUN_2.runId]: RUN_2 },
            activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [
                { run_id: RUN.runId, cell_id: CELL_ID, adopted: true, effective_grant: grant, harness: 'qwen-code', harness_label: 'Qwen Code' },
                { run_id: RUN_2.runId, cell_id: CELL_ID, adopted: false, effective_grant: { ...grant, docker: 'none' }, harness: 'pydantic-ai', harness_label: 'Pydantic AI' },
            ] }],
        });
        mount();
        const [adopted, kept] = rows();
        const chip = within(adopted).getByTestId('status-run-adopted');
        expect(chip.textContent).toBe('Docker full');
        expect(chip).toHaveAttribute('title', 'Docker full adopted for Qwen Code');
        expect(adopted.querySelector('span[title]')!.getAttribute('title')).toContain('· Docker full adopted for Qwen Code');
        expect(within(kept).queryByTestId('status-run-adopted')).toBeNull();
    });

    it('chips a CLI run whose harness CLI runs on this machine, and says so in its title', () => {
        const grant = { docker: 'none', internet: 'full', commands: 'ask' };
        seedPaneStore({
            runs: { [RUN.runId]: RUN, [RUN_2.runId]: RUN_2 },
            activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [
                { run_id: RUN.runId, cell_id: CELL_ID, adopted: false, effective_grant: grant, harness: 'claude-code', harness_label: 'Claude Code', cli_locus: 'host' },
                { run_id: RUN_2.runId, cell_id: CELL_ID, adopted: false, effective_grant: { ...grant, docker: 'full' }, harness: 'codex', harness_label: 'Codex', cli_locus: 'container' },
            ] }],
        });
        mount();
        const [here, container] = rows();
        const chip = within(here).getByTestId('status-run-host-cli');
        expect(chip.textContent).toBe('This machine');
        expect(chip).toHaveAttribute('title', 'Claude Code CLI on this machine');
        expect(here.querySelector('span[title]')!.getAttribute('title')).toContain('· Claude Code CLI on this machine');
        expect(within(here).queryByTestId('status-run-adopted')).toBeNull();
        expect(within(container).queryByTestId('status-run-host-cli')).toBeNull();
    });

    it('lists the live runs with state, name, short id, listening and elapsed; Open shows for a UI file only', () => {
        const started = new Date(Date.now() - 12 * 60_000).toISOString();
        const chat = { ...RUN_2, surfaces: [{ path: '_runs/chat-22222222/chat/c.chat', label: 'Chat' }] };
        seedPaneStore({
            runs: { [RUN.runId]: RUN, [chat.runId]: chat }, runPhases: { [RUN.runId]: { phase: 'awaiting_input' } },
            stoppingRunIds: new Set([chat.runId]),
            annotationContexts: { [CELL_ID]: { [RUN.runId]: ctx(RUN.runId, [], { listening: false }) } },
            activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [{ run_id: RUN.runId, cell_id: CELL_ID, started_at: started }, { run_id: chat.runId, cell_id: CELL_ID, started_at: started, listening: true }] }],
            cellCache: { [CELL_ID]: { files: [{ name: '_runs/chat-22222222/chat/c.chat' }, { name: '_runs/agent-11111111/agent-11111111.run' }] } },
        });
        mount();
        const [agent, other] = rows();
        expect(agent).toHaveAttribute('data-run-id', RUN.runId);
        expect(agent.textContent).toBe('Agent1111111112m');
        expect(agent.querySelector('i')!.className).toContain('bg-brass');
        expect(within(agent).getByTestId('run-listening')).toHaveAttribute('data-listening', 'false');
        expect(within(agent).queryByTestId('status-run-open-ui')).toBeNull();
        expect(other.textContent).toContain('stopping');
        expect(within(other).getByTestId('run-listening')).toHaveAttribute('data-listening', 'true');
        const open = within(other).getByTestId('status-run-open-ui');
        expect(open).toHaveAttribute('aria-label', 'Open c.chat');
        fireEvent.click(open);
        expect(reveal).toHaveBeenCalledWith(CELL_ID, '_runs/chat-22222222/chat/c.chat', { newTab: true });
        expect(store().runsPaneView[CELL_ID]).toBeUndefined();
        expect(store().markFailureSeen).toHaveBeenCalledWith(CELL_ID);
        expect(store().loadCellAnnotations).toHaveBeenCalledWith(CELL_ID);
    });

    it('lists the finished runs dimmed below with how they ended; Clear finished clears them', () => {
        seedPaneStore({
            runs: { [RUN.runId]: RUN },
            finishedRuns: { [CELL_ID]: [finishedRun(RUN_2, { status: 'failed', failureReason: 'boom' }), finishedRun(RUN)] },
        });
        mount();
        expect(rows().map(r => [r.getAttribute('data-run-id'), r.getAttribute('data-ended')])).toEqual([[RUN.runId, null], [RUN_2.runId, 'true']]);
        const ended = rows()[1];
        expect(ended.className).toContain('opacity-[0.55]');
        expect(ended.querySelector('i')!.className).toContain('border-red');
        expect(ended.textContent).toContain('Failed · ');
        expect(ended.title ?? '').toBe('');
        expect(ended.querySelector('span')!.title).toBe('Chat — boom');
    });

    it('a finished row reads the runs row\'s words: a stopped run is Stopped (no failure dot), a completed one Completed', () => {
        seedPaneStore({ finishedRuns: { [CELL_ID]: [finishedRun(RUN, { status: 'stopped' }), finishedRun(RUN_2, { status: 'completed' })] } });
        mount();
        const [stopped, completed] = rows();
        expect(stopped.textContent).toContain('Stopped · ');
        expect(stopped.querySelector('i')!.className).not.toContain('border-red');
        expect(completed.textContent).toContain('Completed · ');
    });

    it('Clear finished empties the finished section', () => {
        seedPaneStore({ finishedRuns: { [CELL_ID]: [finishedRun(RUN)] } });
        mount();
        expect(screen.getByTestId('status-clear-finished').textContent).toBe('Clear finished');
        fireEvent.click(screen.getByTestId('status-clear-finished'));
        expect(screen.queryAllByTestId('status-run-row')).toEqual([]);
        expect(screen.getByText('Nothing is running in this cell.')).toBeInTheDocument();
    });

    it('a row click and ↵ open the run\'s drilldown; ↑↓ wrap; Esc closes', () => {
        seedPaneStore({ runs: { [RUN.runId]: RUN, [RUN_2.runId]: RUN_2 } });
        const { onClose } = mount();
        fireEvent.click(rows()[1]);
        expect(store().runsPaneView[CELL_ID]).toEqual({ view: 'run', runId: RUN_2.runId, option: 'default' });
        const root = screen.getByTestId('status-view');
        root.focus();
        fireEvent.keyDown(root, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(rows()[0]);
        fireEvent.keyDown(rows()[0], { key: 'ArrowUp' });
        expect(document.activeElement).toBe(rows()[1]);
        fireEvent.keyDown(rows()[1], { key: 'ArrowDown' });
        expect(document.activeElement).toBe(rows()[0]);
        fireEvent.keyDown(rows()[0], { key: 'Enter' });
        expect(store().runsPaneView[CELL_ID]).toEqual({ view: 'run', runId: RUN.runId, option: 'default' });
        fireEvent.keyDown(root, { key: 'Enter' });
        fireEvent.keyDown(root, { key: 'Escape' });
        expect(onClose).toHaveBeenCalled();
    });

    it('↑↓ with no rows do nothing', () => {
        mount();
        fireEvent.keyDown(screen.getByTestId('status-view'), { key: 'ArrowDown' });
        expect(document.activeElement).toBe(document.body);
    });

    it('All opens the All view; New run opens the runners panel, and shows only where a run may start', () => {
        mount();
        fireEvent.click(screen.getByTestId('status-all'));
        expect(screen.getByTestId('status-all').textContent).toBe('All');
        expect(store().runsPaneView[CELL_ID]).toEqual({ view: 'all', option: 'default' });
        fireEvent.click(screen.getByTestId('status-new-run'));
        expect(store().openPickSession).toHaveBeenCalledWith(CELL_ID, 'runners');
        cleanup();
        mount({ allowRun: false });
        expect(screen.queryByTestId('status-new-run')).toBeNull();
    });

    it('focus() takes the run the drilldown came back from, else the first row, else the view', () => {
        seedPaneStore({ runs: { [RUN.runId]: RUN, [RUN_2.runId]: RUN_2 } });
        const { ref } = mount({ focusRunId: RUN_2.runId });
        ref.current!.focus();
        expect(document.activeElement).toBe(rows()[1]);
        cleanup();
        const second = mount({ focusRunId: 'gone' });
        second.ref.current!.focus();
        expect(document.activeElement).toBe(rows()[0]);
        cleanup();
        seedPaneStore();
        const empty = mount();
        empty.ref.current!.focus();
        expect(document.activeElement).toBe(screen.getByTestId('status-view'));
    });

    it('the elapsed clock ticks every 15 s', () => {
        vi.useFakeTimers();
        try {
            const started = new Date(Date.now() - 10_000).toISOString();
            seedPaneStore({ runs: { [RUN.runId]: RUN },
                activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [{ run_id: RUN.runId, cell_id: CELL_ID, started_at: started }] }] });
            mount();
            expect(rows()[0].textContent).toContain('<1m');
            act(() => { vi.advanceTimersByTime(60_000); });
            expect(rows()[0].textContent).toMatch(/1m$/);
            expect(rows()[0].textContent).not.toContain('<');
        } finally {
            vi.useRealTimers();
        }
    });
});
