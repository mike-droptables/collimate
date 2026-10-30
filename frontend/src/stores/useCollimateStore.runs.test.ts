// @vitest-environment jsdom
import { fileFixture } from '@/test/fileFixtures';
//
// Store behaviour of several runs per cell that reaches past the run slices:
// pending reveals (a run's reveal_file usually arrives BEFORE the coalesced
// state push that carries the file) and applyCellFiles' per-path lock
// protection.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

vi.mock('@/lib/fileCommands', () => ({ replaceCellFiles: vi.fn(async () => undefined) }));
import { replaceCellFiles } from '@/lib/fileCommands';
import { useCollimateStore } from './useCollimateStore';
import { REVEAL_TTL_MS } from './collimate/runSlice';
import { activeTab, allTabs } from '@/lib/cellLayout';
import type { AppState, Cell, Channel, RunRecord, VirtualFile } from '@/lib/types';

const file = (name: string, content = ''): VirtualFile => (fileFixture(name, content, {language: 'plaintext'}));

const A = '_runs/alpha-11111111';
const B = '_runs/beta-22222222';
const run = (runId: string, label: string): RunRecord => ({
    runId, cellId: 'c1', channel: 'chan', runnerId: label.split('-')[0], runnerName: label,
    generatorName: 'synthetic-gen', label, folder: `_runs/${label}`,
});
const RA = run('run-a', 'alpha-11111111');
const RB = run('run-b', 'beta-22222222');

let stamp = 0;
const channelWith = (files: VirtualFile[]): Channel => ({
    id: 'ch-1', name: 'chan', nav_stacks: [], nav_selection_id: null, main_selection_id: null,
    main_stacks: [{
        id: 'stack-1', name: '',
        cells: [{ id: 'c1', name: 'Cell', files, updated_at: new Date(Date.UTC(2026, 0, 1, 0, 0, ++stamp)).toISOString() } as Cell],
    }],
});
const push = (files: VirtualFile[]) => {
    const state: AppState = { channels: [channelWith(files)], active_channel_id: 'ch-1' };
    useCollimateStore.getState().applyServerState(state);
};

const layout = () => useCollimateStore.getState().cellLayouts.c1;
const paths = () => allTabs(layout()).map(t => (t.kind === 'file' ? t.path : t.kind));

describe('pending reveals', () => {
    beforeEach(() => {
        localStorage.clear();
        useCollimateStore.setState({
            state: null, activeChannel: null, cellCache: {}, cellLayouts: {},
            runs: {}, stoppingRunIds: new Set<string>(),
            pendingReveals: {}, pendingSelectCellId: null,
        });
        useCollimateStore.getState().startRun(RA);
        useCollimateStore.getState().startRun(RB);
        push([file('notes.md')]);
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it('a reveal of a run card is dropped: the card is the run\'s record, never a view', () => {
        push([file(`${A}/alpha-11111111.run`, 'card')]);
        useCollimateStore.getState().queueReveal('c1', { path: `${A}/alpha-11111111.run`, runId: 'run-a', activate: true });
        expect(useCollimateStore.getState().pendingReveals.c1).toBeUndefined();
    });

    it('a reveal arriving before the state update still opens once the file lands', () => {
        useCollimateStore.getState().queueReveal('c1', { path: `${A}/chat.chat`, runId: 'run-a' });
        expect(layout()).toBeUndefined();
        expect(useCollimateStore.getState().pendingReveals.c1).toHaveLength(1);
        push([file('notes.md'), file(`${A}/chat.chat`)]);
        expect(activeTab(layout())).toMatchObject({ kind: 'file', path: `${A}/chat.chat` });
        expect(useCollimateStore.getState().pendingReveals).toEqual({});
        // Layout-only: a run's reveal never selects the cell.
        expect(useCollimateStore.getState().pendingSelectCellId).toBeNull();
    });

    it('a reveal for a file already there applies at once', () => {
        push([file('notes.md'), file(`${A}/chat.chat`)]);
        useCollimateStore.getState().queueReveal('c1', { path: `${A}/chat.chat`, runId: 'run-a' });
        expect(paths()).toEqual([`${A}/chat.chat`]);
        expect(useCollimateStore.getState().pendingReveals).toEqual({});
    });

    it("another run's reveal opens in the background beside its sibling's focused tab", () => {
        push([file('notes.md'), file(`${A}/chat.chat`), file(`${B}/term.terminal`)]);
        useCollimateStore.getState().queueReveal('c1', { path: `${A}/chat.chat`, runId: 'run-a' });
        useCollimateStore.getState().queueReveal('c1', { path: `${B}/term.terminal`, runId: 'run-b' });
        expect(paths()).toEqual([`${A}/chat.chat`, `${B}/term.terminal`]);
        expect(activeTab(layout())).toMatchObject({ path: `${A}/chat.chat` });
    });

    it('activate: true focuses even beside real content', () => {
        push([file('notes.md'), file(`${A}/chat.chat`), file(`${B}/terminal.terminal`)]);
        useCollimateStore.getState().queueReveal('c1', { path: `${A}/chat.chat`, runId: 'run-a' });
        useCollimateStore.getState().queueReveal('c1', { path: `${B}/terminal.terminal`, runId: 'run-b', activate: true });
        expect(activeTab(layout())).toMatchObject({ path: `${B}/terminal.terminal` });
    });

    it('an entry whose file never lands expires', () => {
        vi.useFakeTimers();
        useCollimateStore.getState().queueReveal('c1', { path: `${A}/never.chat`, runId: 'run-a' });
        vi.advanceTimersByTime(REVEAL_TTL_MS + 5);
        expect(useCollimateStore.getState().pendingReveals).toEqual({});
        push([file('notes.md'), file(`${A}/never.chat`)]);
        expect(layout()).toBeUndefined();
    });

    it("a run's end drops its pending reveals", () => {
        useCollimateStore.getState().queueReveal('c1', { path: `${A}/late.chat`, runId: 'run-a' });
        useCollimateStore.getState().queueReveal('c1', { path: `${B}/late.terminal`, runId: 'run-b' });
        useCollimateStore.getState().endRun('run-a', 'completed');
        expect(useCollimateStore.getState().pendingReveals.c1.map(e => e.runId)).toEqual(['run-b']);
    });

    it('a re-queued path replaces its earlier entry', () => {
        useCollimateStore.getState().queueReveal('c1', { path: `${A}/x.chat`, runId: 'run-a' });
        useCollimateStore.getState().queueReveal('c1', { path: `${A}/x.chat`, runId: 'run-a', activate: true });
        expect(useCollimateStore.getState().pendingReveals.c1).toHaveLength(1);
        expect(useCollimateStore.getState().pendingReveals.c1[0].activate).toBe(true);
    });
});

describe('applyCellFiles — run ownership survives run completion', () => {
    beforeEach(() => {
        localStorage.clear(); vi.mocked(replaceCellFiles).mockClear();
        useCollimateStore.setState({state:null,activeChannel:null,cellCache:{},cellLayouts:{},runs:{},stoppingRunIds:new Set<string>(),pendingReveals:{}});
        push([file('notes.md','old'),file(`${A}/chat.chat`,'surface'),file('gone.md')]);
    });
    it.each([true,false])('preserves run-owned bytes and rejects new run paths (live=%s)',async live=>{
        if(live)useCollimateStore.getState().startRun(RA);
        const next=[file('notes.md','new'),file(`${A}/chat.chat`,'tampered'),file(`${B}/new.chat`,'x'),file('added.md','hi')];
        expect(await useCollimateStore.getState().applyCellFiles('c1',next)).toBe(true);
        expect(replaceCellFiles).toHaveBeenCalledOnce();
        const desired=vi.mocked(replaceCellFiles).mock.calls[0][2];
        expect(desired.map(f=>f.name)).toEqual(['notes.md','added.md',`${A}/chat.chat`]);
        expect(desired.find(f=>f.name===`${A}/chat.chat`)?.body_hash).toBe(file('locked','surface').body_hash);
        expect(desired.find(f=>f.name==='notes.md')?.body_hash).toBe(file('notes.md','new').body_hash);
    });
    it('preserves an omitted run file and leaves untouched large metadata unchanged',async()=>{
        const large=file('big.txt','x'.repeat(210000));
        push([file('notes.md'),file(`${A}/chat.chat`,'surface'),large,file('gone.md')]);
        await useCollimateStore.getState().applyCellFiles('c1',[file('notes.md'),large]);
        const desired=vi.mocked(replaceCellFiles).mock.calls[0][2];
        expect(desired.find(f=>f.name==='big.txt')).toBe(large);
        expect(desired.map(f=>f.name)).toContain(`${A}/chat.chat`);
        expect(desired.map(f=>f.name)).not.toContain('gone.md');
    });
});
