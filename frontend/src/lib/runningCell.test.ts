import { describe, it, expect } from 'vitest';
import {
    runsOnCell, cellHasLiveRun, lockOwner, runUiPath,
    dotStatus, isRunPath, runLabelOf, runCardPath, runRecordFrom, isTerminalPhase,
    AWAITING_APPROVAL_PHASE, AWAITING_INPUT_PHASE, type BusyStateSlice,
} from './runningCell';
import type { RunRecord } from './types';

// Synthetic runs: two on cell-1, one on cell-2 in another channel.
const run = (runId: string, cellId: string, label: string, channel = 'Chan One'): RunRecord => ({
    runId, cellId, channel, runnerId: label.split('-')[0], runnerName: `Runner ${label}`,
    generatorName: 'synthetic-gen', label, folder: `_runs/${label}`,
});
const R1 = run('run-1', 'cell-1', 'alpha-11111111');
const R2 = run('run-2', 'cell-1', 'beta-22222222');
const R3 = run('run-3', 'cell-2', 'gamma-33333333', 'other');

const slice = (over: Partial<BusyStateSlice> = {}): BusyStateSlice => ({
    runs: { 'run-1': R1, 'run-2': R2, 'run-3': R3 },
    stoppingRunIds: new Set<string>(),
    runPhases: {},
    ...over,
});


describe('run-path helpers', () => {
    it('recognises run folders and their labels', () => {
        expect(isRunPath('_runs/alpha-11111111/chat.chat')).toBe(true);
        expect(isRunPath('_runs')).toBe(true);
        expect(isRunPath('_runsx/a')).toBe(false);
        expect(isRunPath('src/_runs/a')).toBe(false);
        expect(runLabelOf('_runs/alpha-11111111/chat/conversation.chat')).toBe('alpha-11111111');
        expect(runLabelOf('notes.md')).toBeNull();
        expect(runLabelOf('_runs/')).toBeNull();
        expect(runCardPath(R1)).toBe('_runs/alpha-11111111/alpha-11111111.run');
    });

    it('terminal phases', () => {
        expect(isTerminalPhase('completed')).toBe(true);
        expect(isTerminalPhase('running')).toBe(false);
    });
});



describe('runsOnCell / cellHasLiveRun', () => {
    it('lists the active and stopping runs of the cell, in start order', () => {
        const s = slice({ stoppingRunIds: new Set(['run-2']) });
        expect(runsOnCell(s, 'cell-1').map(r => r.runId)).toEqual(['run-1', 'run-2']);
        expect(runsOnCell(s, 'cell-2').map(r => r.runId)).toEqual(['run-3']);
        expect(cellHasLiveRun(s, 'cell-1')).toBe(true);
    });

    it('a removed run is not live', () => {
        const s = slice({runs: {}});
        expect(runsOnCell(s, 'cell-1')).toEqual([]);
        expect(cellHasLiveRun(s, 'cell-1')).toBe(false);
        expect(cellHasLiveRun(s, 'cell-9')).toBe(false);
    });

    it('a run idling in awaiting_input is still live', () => {
        const idle = slice({
            runPhases: { 'run-1': { phase: AWAITING_INPUT_PHASE } },
        });
        expect(cellHasLiveRun(idle, 'cell-1')).toBe(true);
    });
});

describe('lockOwner', () => {
    const s = slice({ });

    it("returns the live run whose folder holds the path, on that cell only", () => {
        expect(lockOwner(s, 'cell-1', '_runs/alpha-11111111/chat/conversation.chat')?.runId).toBe('run-1');
        expect(lockOwner(s, 'cell-1', '_runs/beta-22222222/term/term.terminal')?.runId).toBe('run-2');
        expect(lockOwner(s, 'cell-2', '_runs/alpha-11111111/chat/conversation.chat')).toBeNull();
    });

    it('unlocked: a work file, a dead folder, a folder-name prefix, no path', () => {
        expect(lockOwner(s, 'cell-1', 'notes.md')).toBeNull();
        expect(lockOwner(s, 'cell-1', '_runs/dead-44444444/x.chat')).toBeNull();
        expect(lockOwner(s, 'cell-1', '_runs/alpha-1111111/x.chat')).toBeNull();
        expect(lockOwner(s, 'cell-1', null)).toBeNull();
        expect(lockOwner(slice({runs:{}}), 'cell-1', '_runs/alpha-11111111/x.chat')).toBeNull();
    });
});

describe('dotStatus — precedence over the live runs of a cell', () => {
    const phases = (p1?: string, p2?: string) => slice({
        runPhases: {
            ...(p1 ? { 'run-1': { phase: p1 } } : {}),
            ...(p2 ? { 'run-2': { phase: p2 } } : {}),
        },
    });

    it('idle → null', () => {
        expect(dotStatus(slice({runs:{}}), 'cell-1')).toBeNull();
    });

    it('awaiting approval beats everything', () => {
        expect(dotStatus(phases(AWAITING_APPROVAL_PHASE, 'running'), 'cell-1')).toBe('awaiting_approval');
    });

    it('busy running beats spinning up and awaiting input; no phase counts as running', () => {
        expect(dotStatus(phases('running', 'spinning_up'), 'cell-1')).toBe('running');
        expect(dotStatus(phases(undefined, AWAITING_INPUT_PHASE), 'cell-1')).toBe('running');
    });

    it('spinning up (or queued) beats awaiting input', () => {
        expect(dotStatus(phases('spinning_up', AWAITING_INPUT_PHASE), 'cell-1')).toBe('spinning_up');
        expect(dotStatus(phases('queued', AWAITING_INPUT_PHASE), 'cell-1')).toBe('spinning_up');
    });

    it('awaiting input when every run idles', () => {
        expect(dotStatus(phases(AWAITING_INPUT_PHASE, AWAITING_INPUT_PHASE), 'cell-1')).toBe('awaiting_input');
    });

    it('a stopping run is ignored while a sibling lives; all stopping → stopping', () => {
        const s = slice({ stoppingRunIds: new Set(['run-1']),
            runPhases: { 'run-1': { phase: 'running' }, 'run-2': { phase: AWAITING_INPUT_PHASE } },
        });
        expect(dotStatus(s, 'cell-1')).toBe('awaiting_input');
        expect(dotStatus(slice({ stoppingRunIds: new Set(['run-1', 'run-2']) }), 'cell-1')).toBe('stopping');
    });
});

describe('runRecordFrom', () => {
    it('maps the server run shape onto a RunRecord', () => {
        expect(runRecordFrom({
            run_id: 'r', cell_id: 'c', runner_id: null, runner_name: null,
            label: 'sub-12345678', folder: '_runs/sub-12345678', generator_name: 'g',
        }, 'chan')).toEqual({
            runId: 'r', cellId: 'c', channel: 'chan', runnerId: null, runnerName: null,
            generatorName: 'g', label: 'sub-12345678', folder: '_runs/sub-12345678',
        });
    });
});


describe('D22 served file metadata',()=>{
    const served={...R1,surfaces:[{path:'report.chat',label:'Report'}]};
    it('maps the served surface path (and the run folder) to the serving run, nothing else',()=>{
        const s=slice({runs:{'run-1':served}});
        expect(lockOwner(s,'cell-1','report.chat')).toEqual(served);
        expect(lockOwner(s,'cell-1','./report.chat')).toEqual(served);
        expect(lockOwner(s,'cell-2','report.chat')).toBeNull();
        expect(lockOwner(s,'cell-1','report.notes')).toBeNull();
        expect(lockOwner(s,'cell-1','Report')).toBeNull();
    });
});

describe('runUiPath',()=>{
    const run={folder:'_runs/alpha-11111111',label:'alpha-11111111'};
    it('names the surface when the cell holds it',()=>{
        expect(runUiPath({...run,surfaces:[{path:'_runs/alpha-11111111/chat/c.chat',label:'Chat'}]},
            ['_runs/alpha-11111111/chat/c.chat','_runs/alpha-11111111/a.md'])).toBe('_runs/alpha-11111111/chat/c.chat');
    });
    it('else the first folder file that is neither the run card nor a folder keeper',()=>{
        expect(runUiPath({...run,surfaces:[{path:'_runs/alpha-11111111/gone.chat',label:'Chat'}]},
            ['notes.md','_runs/alpha-11111111/alpha-11111111.run','_runs/alpha-11111111/b/.collkeep','_runs/alpha-11111111/z.md','_runs/alpha-11111111/b.json']))
            .toBe('_runs/alpha-11111111/b.json');
    });
    it('else null',()=>{
        expect(runUiPath(run,['notes.md','_runs/alpha-11111111/alpha-11111111.run','_runs/beta-22222222/x.md'])).toBeNull();
    });
});
