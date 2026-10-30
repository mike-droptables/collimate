import { fixtureBodyHash } from '@/test/fileFixtures';
import { fileFixture } from '@/test/fileFixtures';
import {act,renderHook,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {useCellValidation} from './useCellValidation';
import {fetchCellValidation,type CellValidation} from '@/lib/definitionApi';
import type {VirtualFile} from '@/lib/types';
vi.mock('@/lib/definitionApi',()=>({fetchCellValidation:vi.fn()}));
const files:VirtualFile[]=[fileFixture('a.note', 'one', {language:'text', timestamp:'one'})];
const savedFiles:Record<string,Date>={};
const verdict=(cell:string,pending=false):CellValidation=>({cell_id:cell,source_version_id:'v',source_revision:'revision',pending,verdicts:pending?{}:{'a.note':{valid:false,error:'Missing heading'}}});
afterEach(()=>{vi.resetAllMocks();vi.useRealTimers();});

it('polls pending server validation and rejects an old cell reply __D10',async()=>{
 vi.useFakeTimers();
 let oldReply!:(value:CellValidation)=>void;
 vi.mocked(fetchCellValidation).mockImplementationOnce(()=>new Promise(resolve=>{oldReply=resolve;}))
  .mockResolvedValueOnce(verdict('new',true)).mockResolvedValueOnce(verdict('new'));
 const view=renderHook(({id})=>useCellValidation('channel',id,files,true,savedFiles),{initialProps:{id:'old'}});
 view.rerender({id:'new'});
 await act(async()=>{});expect(view.result.current?.pending).toBe(true);
 await act(async()=>{oldReply(verdict('old'));});expect(view.result.current?.cell_id).toBe('new');
 await act(async()=>{await vi.advanceTimersByTimeAsync(500);});
 expect(view.result.current?.verdicts['a.note'].error).toBe('Missing heading');
 view.unmount();await vi.advanceTimersByTimeAsync(5000);expect(fetchCellValidation).toHaveBeenCalledTimes(3);
});

it('clears verdicts immediately when local files change and does not observe a hidden cell __D10',async()=>{
 vi.mocked(fetchCellValidation).mockResolvedValueOnce(verdict('cell')).mockImplementationOnce(()=>new Promise(()=>{}));
 const view=renderHook(({visible,current})=>useCellValidation('channel','cell',current,visible,savedFiles),{initialProps:{visible:false,current:files}});
 expect(fetchCellValidation).not.toHaveBeenCalled();
 view.rerender({visible:true,current:files});await waitFor(()=>expect(view.result.current?.pending).toBe(false));
 view.rerender({visible:true,current:[{...files[0],body_hash: fixtureBodyHash('two'),timestamp:'two'}]});
 expect(view.result.current).toBeNull();
 view.unmount();
});

it('refreshes a completed verdict when an accepted file receipt arrives __D10',async()=>{
 const old={...verdict('cell'),verdicts:{'a.note':{valid:true}}};
 vi.mocked(fetchCellValidation).mockResolvedValueOnce(old).mockResolvedValueOnce(old).mockResolvedValueOnce(verdict('cell'));
 const view=renderHook(({current,saved})=>useCellValidation('channel','cell',current,true,saved),{initialProps:{current:files,saved:{} as Record<string,Date>}});
 await waitFor(()=>expect(view.result.current?.verdicts['a.note'].valid).toBe(true));
 const edited=[{...files[0],body_hash:fixtureBodyHash('invalid edit'),file_revision:2}];
 view.rerender({current:edited,saved:{}});
 await waitFor(()=>expect(fetchCellValidation).toHaveBeenCalledTimes(2));
 await waitFor(()=>expect(view.result.current?.verdicts['a.note'].valid).toBe(true));
 // Receipt bookkeeping refreshes the advisory verdict for the same metadata.
 view.rerender({current:edited,saved:{'a.note':new Date()}});
 expect(view.result.current).toBeNull();
 await waitFor(()=>expect(view.result.current?.verdicts['a.note'].valid).toBe(false));
 expect(fetchCellValidation).toHaveBeenCalledTimes(3);
});
