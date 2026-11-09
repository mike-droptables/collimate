// The status section: the cell's live runs (state, name and short id,
// whether each reads its messages, chips, elapsed, its UI file's Open, the
// annotation icon), the runs that ended this session below them until
// Clear finished, a row's expanded body (state, Stop and the live options
// while it runs; how it ended once it has), the footer's Run, All and Stop all,
// Run, its keys and where Esc hands the keyboard.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const reveal = vi.hoisted(() => vi.fn());
const stopRun = vi.hoisted(() => vi.fn());
vi.mock('@/lib/revealFile', () => ({ revealCellFile: reveal }));
vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    stopRun,
}));

import { StatusSection } from './StatusSection';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { CELL_ID, RUN, RUN_2, ctx, finishedRun, seedPaneStore } from '@/test/annotationFixtures';
import type { RunOption } from '@/lib/api';
import { STOP_LOCK_ARMS_ATTR } from '@/hooks/usePostStopLock';
import { POST_STOP_LOCK_MS } from '@/lib/runStop';

const store = () => useCollimateStore.getState();
const rows = () => screen.getAllByTestId('status-run-row');
const section = () => screen.getByTestId('status-section');
const detailOf = (runId: string) => document.querySelector<HTMLElement>(`[data-testid="status-run-detail"][data-run-id="${runId}"]`);
const mount = (props: Partial<Parameters<typeof StatusSection>[0]> = {}) =>
    render(<StatusSection cellId={CELL_ID} allowRun {...props} />);
const seed = (over: Record<string, unknown> = {}) => seedPaneStore({
    openPickSession: vi.fn(),
    statusSection: { [CELL_ID]: { open: true, expandedRunId: null } },
    ...over,
});

const MODEL: RunOption = { id: 'model', name: 'Model', type: 'select', currentValue: 'a', options: [{ value: 'a', name: 'A' }, { value: 'b', name: 'B' }] };

beforeEach(() => {
    reveal.mockReset();
    stopRun.mockReset().mockResolvedValue(undefined);
    seed();
});
afterEach(cleanup);

describe('StatusSection rows', () => {
    it('is a region of the cell, a nav-keys owner, capped at 40% and ruled off from the panes below', () => {
        mount();
        const root = section();
        expect(root).toHaveAttribute('role', 'region');
        expect(root).toHaveAttribute('aria-label', 'Runs in this cell');
        expect(root.hasAttribute('data-owns-nav-keys')).toBe(true);
        expect(root.className.split(/\s+/)).toEqual(expect.arrayContaining(['flex-none', 'max-h-[40%]', 'border-b', 'border-line']));
    });

    it('chips a run whose grant was raised to its harness\'s minimum, and says so in its title', () => {
        const grant = { docker: 'full', internet: 'full', commands: 'deny' };
        seed({
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
        seed({
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
        expect(within(container).queryByTestId('status-run-host-cli')).toBeNull();
    });

    it('chips a run whose .git is read-only or missing, with the host\'s sentence', () => {
        const readonly = 'git is read-only in this run: run 22222222 (Chat) holds this cell\'s git; Duplicate the cell to commit';
        const limited = 'repositories left out: a docker: limited run gets no .git';
        seed({
            runs: { [RUN.runId]: RUN, [RUN_2.runId]: RUN_2 },
            activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [
                { run_id: RUN.runId, cell_id: CELL_ID, git: { repositories: ['proj'], access: 'readonly', reason: readonly } },
                { run_id: RUN_2.runId, cell_id: CELL_ID, git: { repositories: ['.'], access: 'none', reason: limited } },
            ] }],
        });
        mount();
        const [sibling, none] = rows();
        expect(within(sibling).getByTestId('status-run-readonly-git')).toHaveAttribute('title', readonly);
        expect(sibling.querySelector('span[title]')!.getAttribute('title')).toContain(`· ${readonly}`);
        expect(within(none).getByTestId('status-run-no-git').textContent).toBe('No git');
        expect(within(none).queryByTestId('status-run-readonly-git')).toBeNull();
    });

    it('lists the live runs with state, name, short id, listening and elapsed; Open shows for a UI file only; mounting sees the last failure and loads the annotations', () => {
        const started = new Date(Date.now() - 12 * 60_000).toISOString();
        const chat = { ...RUN_2, surfaces: [{ path: '_runs/chat-22222222/chat/c.chat', label: 'Chat' }] };
        seed({
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
        // Open is its own gesture: the row stays collapsed.
        expect(other).not.toHaveAttribute('data-expanded');
        expect(store().annotationPaneView[CELL_ID]).toBeUndefined();
        expect(store().markFailureSeen).toHaveBeenCalledWith(CELL_ID);
        expect(store().loadCellAnnotations).toHaveBeenCalledWith(CELL_ID);
    });

    it('lists the finished runs dimmed below with how they ended', () => {
        seed({
            runs: { [RUN.runId]: RUN },
            finishedRuns: { [CELL_ID]: [finishedRun(RUN_2, { status: 'failed', failureReason: 'boom' }), finishedRun(RUN)] },
        });
        mount();
        expect(rows().map(r => [r.getAttribute('data-run-id'), r.getAttribute('data-ended')])).toEqual([[RUN.runId, null], [RUN_2.runId, 'true']]);
        const ended = rows()[1];
        expect(ended.className).toContain('opacity-[0.55]');
        expect(ended.querySelector('i')!.className).toContain('border-red');
        expect(ended.textContent).toContain('Failed · ');
        expect(ended.querySelector('span')!.title).toBe('Chat — boom');
    });

    it('a finished row reads the runs row\'s words: a stopped run is Stopped (no failure dot), a completed one Completed', () => {
        seed({ finishedRuns: { [CELL_ID]: [finishedRun(RUN, { status: 'stopped' }), finishedRun(RUN_2, { status: 'completed' })] } });
        mount();
        const [stopped, completed] = rows();
        expect(stopped.textContent).toContain('Stopped · ');
        expect(stopped.querySelector('i')!.className).not.toContain('border-red');
        expect(completed.textContent).toContain('Completed · ');
    });

    it('Clear finished empties the finished section', () => {
        seed({ finishedRuns: { [CELL_ID]: [finishedRun(RUN)] } });
        mount();
        expect(screen.getByTestId('status-clear-finished').textContent).toBe('Clear finished');
        fireEvent.click(screen.getByTestId('status-clear-finished'));
        expect(screen.queryAllByTestId('status-run-row')).toEqual([]);
        expect(screen.getByText('Nothing is running in this cell.')).toBeInTheDocument();
    });

    it('the elapsed clock ticks every 15 s', () => {
        vi.useFakeTimers();
        try {
            const started = new Date(Date.now() - 10_000).toISOString();
            seed({ runs: { [RUN.runId]: RUN },
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

describe('StatusSection expanded rows', () => {
    it('a click expands a row in place and a second collapses it; one row at a time', () => {
        seed({ runs: { [RUN.runId]: RUN, [RUN_2.runId]: RUN_2 } });
        mount();
        expect(detailOf(RUN.runId)).toBeNull();
        fireEvent.click(rows()[0]);
        expect(store().statusSection[CELL_ID].expandedRunId).toBe(RUN.runId);
        expect(rows()[0]).toHaveAttribute('data-expanded', 'true');
        expect(rows()[0]).toHaveAttribute('aria-expanded', 'true');
        expect(detailOf(RUN.runId)).not.toBeNull();
        fireEvent.click(rows()[1]);
        expect(detailOf(RUN.runId)).toBeNull();
        expect(detailOf(RUN_2.runId)).not.toBeNull();
        fireEvent.click(rows()[1]);
        expect(store().statusSection[CELL_ID].expandedRunId).toBeNull();
        expect(screen.queryAllByTestId('status-run-detail')).toEqual([]);
    });

    it('a live run\'s body holds its state, its grant line, Stop and its live options; none of them show collapsed', () => {
        const grant = { docker: 'full', internet: 'full', commands: 'deny' };
        seed({
            runs: { [RUN.runId]: RUN }, runPhases: { [RUN.runId]: { phase: 'awaiting_input' } },
            activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [
                { run_id: RUN.runId, cell_id: CELL_ID, adopted: true, effective_grant: grant, harness_label: 'Qwen Code', options: [MODEL], options_rev: 1 },
            ] }],
        });
        mount();
        expect(screen.queryByTestId('run-stop')).toBeNull();
        expect(screen.queryByTestId('run-options')).toBeNull();
        fireEvent.click(rows()[0]);
        const body = detailOf(RUN.runId)!;
        expect(within(body).getByTestId('status-run-state').textContent).toBe('awaiting your message');
        expect(body.textContent).toContain('Docker full adopted for Qwen Code');
        expect(within(body).getByTestId('run-stop')).toHaveAccessibleName('Stop ‘Agent’');
        expect(within(body).getByTestId('run-options')).toBeTruthy();
        expect(within(body).getByTestId('run-option-model')).toBeTruthy();
        // Stop and the options sit in the body, after the state.
        const state = within(body).getByTestId('status-run-state');
        expect(state.compareDocumentPosition(within(body).getByTestId('run-stop')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });

    it('a live run with no published options shows Stop alone', () => {
        seed({ runs: { [RUN.runId]: RUN }, statusSection: { [CELL_ID]: { open: true, expandedRunId: RUN.runId } } });
        mount();
        const body = detailOf(RUN.runId)!;
        expect(within(body).getByTestId('run-stop')).toBeTruthy();
        expect(within(body).queryByTestId('run-options')).toBeNull();
    });

    it('Stop in the body grace-stops the run without collapsing the row', () => {
        seed({ runs: { [RUN.runId]: RUN }, statusSection: { [CELL_ID]: { open: true, expandedRunId: RUN.runId } } });
        mount();
        fireEvent.click(within(detailOf(RUN.runId)!).getByTestId('run-stop'));
        expect(stopRun).toHaveBeenCalledWith(RUN.channel, RUN.runId);
        expect(store().stoppingRunIds.has(RUN.runId)).toBe(true);
        expect(store().statusSection[CELL_ID].expandedRunId).toBe(RUN.runId);
    });

    it('a finished run\'s body says how and when it ended, with no Stop or options', () => {
        seed({ finishedRuns: { [CELL_ID]: [finishedRun(RUN, { status: 'failed', failureReason: 'boom' })] } });
        mount();
        fireEvent.click(rows()[0]);
        const body = detailOf(RUN.runId)!;
        expect(within(body).getByTestId('status-run-state').textContent).toBe('Failed');
        expect(body.textContent).toContain('boom');
        expect(body.textContent).toMatch(/Ended \d\d:\d\d/);
        expect(within(body).queryByTestId('run-stop')).toBeNull();
        expect(within(body).queryByTestId('run-options')).toBeNull();
    });
});

describe('StatusSection annotation icon', () => {
    it('every row, live or finished, opens the annotation pane on that run, without expanding the row', () => {
        seed({ runs: { [RUN.runId]: RUN }, finishedRuns: { [CELL_ID]: [finishedRun(RUN_2)] } });
        mount();
        const [live, ended] = rows();
        const icon = within(live).getByTestId('status-run-annotate');
        expect(icon).toHaveAttribute('title', 'Open in the annotation pane');
        expect(icon.querySelector('svg.lucide-message-square')).not.toBeNull();
        fireEvent.click(icon);
        expect(store().annotationPaneView[CELL_ID]).toEqual({ view: 'run', runId: RUN.runId, option: 'default' });
        expect(store().cellLayouts[CELL_ID].runsPane.open).toBe(true);
        expect(store().annotationPaneFocus[CELL_ID]).toMatchObject({ target: 'view' });
        expect(live).not.toHaveAttribute('data-expanded');
        fireEvent.click(within(ended).getByTestId('status-run-annotate'));
        expect(store().annotationPaneView[CELL_ID]).toEqual({ view: 'run', runId: RUN_2.runId, option: 'default' });
        expect(ended).not.toHaveAttribute('data-expanded');
    });
});

describe('StatusSection footer', () => {
    it('Stop all N stops every live run not already stopping, and shows only while there is one', () => {
        const third = { ...RUN, runId: 'run-3', label: 'agent-33333333' };
        seed({ runs: { [RUN.runId]: RUN, [RUN_2.runId]: RUN_2, [third.runId]: third }, stoppingRunIds: new Set([third.runId]) });
        mount();
        const stopAll = screen.getByTestId('status-stop-all');
        expect(stopAll.textContent).toBe('Stop all 2');
        fireEvent.click(stopAll);
        expect(stopRun.mock.calls.map(c => c.slice(0, 2))).toEqual([[RUN.channel, RUN.runId], [RUN_2.channel, RUN_2.runId]]);
        expect([...store().stoppingRunIds].sort()).toEqual([RUN.runId, RUN_2.runId, third.runId].sort());
        expect(screen.queryByTestId('status-stop-all')).toBeNull();
    });

    it('a Stop arms the post-stop lock over the section: the burst\'s next clicks land nowhere, then rows answer again', () => {
        vi.useFakeTimers();
        try {
            const third = { ...RUN, runId: 'run-3', label: 'agent-33333333' };
            seed({ runs: { [RUN.runId]: RUN, [third.runId]: third }, stoppingRunIds: new Set([third.runId]) });
            mount();
            expect(screen.getByTestId('status-stop-all')).toHaveAttribute(STOP_LOCK_ARMS_ATTR);
            fireEvent.click(screen.getByTestId('status-stop-all'));
            const row = rows().find(r => r.getAttribute('data-run-id') === third.runId)!;
            fireEvent.click(row);
            expect(row).not.toHaveAttribute('data-expanded');
            // Every swallowed click re-arms it: the lock runs from the last.
            act(() => { vi.advanceTimersByTime(POST_STOP_LOCK_MS - 1); });
            fireEvent.click(row);
            act(() => { vi.advanceTimersByTime(POST_STOP_LOCK_MS - 1); });
            fireEvent.click(row);
            expect(row).not.toHaveAttribute('data-expanded');
            act(() => { vi.advanceTimersByTime(POST_STOP_LOCK_MS); });
            fireEvent.click(row);
            expect(row).toHaveAttribute('data-expanded', 'true');
        } finally {
            vi.useRealTimers();
        }
    });

    it('a refused stop clears its stopping mark', async () => {
        stopRun.mockRejectedValueOnce(new Error('gone'));
        const error = vi.spyOn(console, 'error').mockImplementation(() => {});
        seed({ runs: { [RUN.runId]: RUN } });
        mount();
        fireEvent.click(screen.getByTestId('status-stop-all'));
        await waitFor(() => expect(store().stoppingRunIds.has(RUN.runId)).toBe(false));
        error.mockRestore();
    });

    it('Run then All sit at the left, Stop all at the right; no live run, no Stop all', () => {
        seed({ runs: { [RUN.runId]: RUN }, stoppingRunIds: new Set() });
        mount();
        expect(screen.getByTestId('status-new-run').nextElementSibling).toBe(screen.getByTestId('status-all'));
        expect(screen.getByTestId('status-all').nextElementSibling).toBe(screen.getByTestId('status-stop-all'));
        expect(screen.getByTestId('status-stop-all').className).toContain('ml-auto');
        expect(screen.getByTestId('status-new-run').className).not.toContain('ml-auto');
        expect(screen.getByTestId('status-all').className).not.toContain('ml-auto');
        cleanup();
        seed({ finishedRuns: { [CELL_ID]: [finishedRun(RUN)] } });
        mount();
        expect(screen.queryByTestId('status-stop-all')).toBeNull();
        expect(screen.getByTestId('status-new-run').nextElementSibling).toBe(screen.getByTestId('status-all'));
    });

    it('All opens the annotation pane on All; Run opens the runners panel, and shows only where a run may start', () => {
        mount();
        fireEvent.click(screen.getByTestId('status-all'));
        expect(screen.getByTestId('status-all').textContent).toBe('All');
        expect(store().annotationPaneView[CELL_ID]).toEqual({ view: 'all', option: 'default' });
        expect(store().cellLayouts[CELL_ID].runsPane.open).toBe(true);
        expect(screen.getByTestId('status-new-run').textContent).toBe('Run');
        fireEvent.click(screen.getByTestId('status-new-run'));
        expect(store().openPickSession).toHaveBeenCalledWith(CELL_ID, 'runners');
        cleanup();
        mount({ allowRun: false });
        expect(screen.queryByTestId('status-new-run')).toBeNull();
    });
});

describe('StatusSection keys and focus', () => {
    it('↑↓ wrap over the rows, ↵ expands the focused one, a key in an expanded row\'s select stays its own', () => {
        seed({
            runs: { [RUN.runId]: RUN, [RUN_2.runId]: RUN_2 },
            activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [{ run_id: RUN.runId, cell_id: CELL_ID, options: [MODEL], options_rev: 1 }] }],
        });
        mount();
        const root = section();
        root.focus();
        fireEvent.keyDown(root, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(rows()[0]);
        fireEvent.keyDown(rows()[0], { key: 'ArrowUp' });
        expect(document.activeElement).toBe(rows()[1]);
        fireEvent.keyDown(rows()[1], { key: 'ArrowDown' });
        expect(document.activeElement).toBe(rows()[0]);
        fireEvent.keyDown(rows()[0], { key: 'Enter' });
        expect(store().statusSection[CELL_ID].expandedRunId).toBe(RUN.runId);
        // ↵ on the section itself expands nothing.
        fireEvent.keyDown(root, { key: 'Enter' });
        expect(store().statusSection[CELL_ID].expandedRunId).toBe(RUN.runId);
        const select = within(detailOf(RUN.runId)!).getByRole('combobox');
        select.focus();
        const down = fireEvent.keyDown(select, { key: 'ArrowDown' });
        expect(down).toBe(true);
        expect(document.activeElement).toBe(select);
    });

    it('↑↓ with no rows do nothing; the empty section says so', () => {
        mount();
        expect(screen.getByText('Nothing is running in this cell.')).toBeInTheDocument();
        fireEvent.keyDown(section(), { key: 'ArrowDown' });
        expect(document.activeElement).toBe(document.body);
    });

    it('Esc closes the section and hands the keyboard to the header\'s status toggle', () => {
        seed({ runs: { [RUN.runId]: RUN } });
        render(
            <div data-cell-id={CELL_ID}>
                <button type="button" data-testid="cell-status-toggle">status</button>
                <StatusSection cellId={CELL_ID} allowRun />
            </div>,
        );
        rows()[0].focus();
        fireEvent.keyDown(rows()[0], { key: 'Escape' });
        expect(store().statusSection[CELL_ID].open).toBe(false);
        expect(document.activeElement).toBe(screen.getByTestId('cell-status-toggle'));
    });

    it('in the full-window view Esc hands the keyboard to the tab strip\'s status toggle', () => {
        render(
            <div data-cell-id={CELL_ID}>
                <button type="button" data-testid="cell-status-toggle">header</button>
                <div data-full-window>
                    <button type="button" data-testid="cell-status-toggle-strip">strip</button>
                    <StatusSection cellId={CELL_ID} allowRun />
                </div>
            </div>,
        );
        section().focus();
        fireEvent.keyDown(section(), { key: 'Escape' });
        expect(store().statusSection[CELL_ID].open).toBe(false);
        expect(document.activeElement).toBe(screen.getByTestId('cell-status-toggle-strip'));
    });

    it('a fresh focus request puts the keyboard on the first row, else on the section; a stale one takes nothing', async () => {
        seed({ runs: { [RUN.runId]: RUN, [RUN_2.runId]: RUN_2 }, statusSectionFocus: { [CELL_ID]: { seq: 1, at: Date.now() } } });
        mount();
        await waitFor(() => expect(document.activeElement).toBe(rows()[0]));
        cleanup();
        seed({ statusSectionFocus: { [CELL_ID]: { seq: 1, at: Date.now() } } });
        mount();
        await waitFor(() => expect(document.activeElement).toBe(section()));
        cleanup();
        (document.activeElement as HTMLElement | null)?.blur();
        seed({ runs: { [RUN.runId]: RUN }, statusSectionFocus: { [CELL_ID]: { seq: 4, at: Date.now() - 10_000 } } });
        mount();
        await new Promise(r => requestAnimationFrame(() => r(null)));
        expect(document.activeElement).toBe(document.body);
        // A newer request while open takes the keys.
        act(() => { store().openStatusSection(CELL_ID); });
        await waitFor(() => expect(document.activeElement).toBe(rows()[0]));
    });
});
