// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest';
import { useCollimateStore } from './useCollimateStore';
import { cellHasLiveRun, lockOwner } from '@/lib/runningCell';
import type { RunRecord } from '@/lib/types';

const run: RunRecord = {runId:'scale-run',cellId:'scale-cell',channel:'scale',runnerId:null,runnerName:null,generatorName:'test',label:'test-12345678',folder:'_runs/test-12345678'};
beforeEach(()=>useCollimateStore.setState({runs:{},stoppingRunIds:new Set(),runLogs:{}}));
describe('bounded run state',()=>{
    it('the authoritative run map drives live predicates after recovery __F0508',()=>{
        useCollimateStore.getState().setRun(run);
        expect(cellHasLiveRun(useCollimateStore.getState(),run.cellId)).toBe(true);
        expect(lockOwner(useCollimateStore.getState(),run.cellId,run.folder+'/result.txt')).toEqual(run);
        useCollimateStore.getState().endRun(run.runId);
        expect(cellHasLiveRun(useCollimateStore.getState(),run.cellId)).toBe(false);
    });
    it('retains the latest 5000 lines across batch and single append __F0804_F0754_F0529_F0480',()=>{
        const s=useCollimateStore.getState();
        s.appendLogs(run.runId,Array.from({length:7000},(_,i)=>'line-'+i));
        expect(useCollimateStore.getState().runLogs[run.runId]).toHaveLength(5000);
        expect(useCollimateStore.getState().runLogs[run.runId][0]).toBe('line-2000');
        s.appendLog(run.runId,'last');
        expect(useCollimateStore.getState().runLogs[run.runId].at(-1)).toBe('last');
        expect(useCollimateStore.getState().runLogs[run.runId][0]).toBe('line-2001');
    });
});
