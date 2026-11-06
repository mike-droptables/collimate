import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useCellDiff } from './useCellDiff';
vi.mock('@/lib/fileFetchCache', async original => {
    const actual = await original<typeof import('@/lib/fileFetchCache')>();
    return { ...actual, fileFetchCache: actual.createFileFetchCache() };
});
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Cell, Channel, VirtualFile } from '@/lib/types';

let epoch = 0;
const vf = (name:string,hash:string,revision=1):VirtualFile=>({name,language:'plaintext',body_hash:`${epoch}:${hash}`,file_revision:revision,encoding:'utf-8',writer_id:'fixture',edit_seq:revision});
const cell=(id:string,files:VirtualFile[]):Cell=>({id,name:id,files});
const channel=(cells:Cell[]):Channel=>({id:'ch',name:'chan',nav_stacks:[],nav_selection_id:null,main_selection_id:null,main_stacks:cells.map((c,i)=>({id:`s${i}`,name:'',cells:[c]}))});
const response=(body:string,hash:string,revision=1)=>({ok:true,headers:new Headers({'X-File-Revision':String(revision),'X-Body-Hash':`${epoch}:${hash}`}),text:async()=>body});
function setup(base:Cell,right=cell('right',[])){
    useCollimateStore.setState({activeChannel:channel([base,right]),cellDiffs:{[right.id]:base.id},cellCache:{}});
    return right;
}

describe('revision-pinned cell diffs',()=>{
    const fetchMock=vi.fn();
    beforeEach(()=>{epoch++;fetchMock.mockReset();vi.stubGlobal('fetch',fetchMock);});
    afterEach(()=>vi.unstubAllGlobals());
    it('is idle for a cell without a diff',()=>{
        useCollimateStore.setState({activeChannel:channel([]),cellDiffs:{},cellCache:{}});
        const hook=renderHook(()=>useCellDiff(cell('x',[])));
        expect(hook.result.current).toBeNull();expect(fetchMock).not.toHaveBeenCalled();
    });
    it('hydrates every text size and pins its requested revision',async()=>{
        fetchMock.mockImplementation((url:string)=>Promise.resolve(url.includes('small')?response('hi','small'):response('x'.repeat(60000),'large',4)));
        const right=setup(cell('base',[vf('small.txt','small'),vf('large.txt','large',4)]));
        const hook=renderHook(()=>useCellDiff(right));
        expect(hook.result.current?.baseFiles).toBeNull();
        await waitFor(()=>expect(hook.result.current?.baseFiles?.['large.txt']).toHaveLength(60000));
        expect(hook.result.current?.baseFiles?.['small.txt']).toBe('hi');
        expect(fetchMock.mock.calls.map(call=>call[0])).toEqual(expect.arrayContaining([expect.stringContaining('small.txt?revision=1'),expect.stringContaining('large.txt?revision=4')]));
    });
    it('keeps hydrated bodies out of store metadata on both sides',async()=>{
        fetchMock.mockImplementation((url:string)=>Promise.resolve(url.includes('/base/')?response('before','old'):response('after','new')));
        const base=cell('base',[vf('a.txt','old')]);const right=setup(base,cell('right',[vf('a.txt','new')]));
        const hook=renderHook(()=>useCellDiff(right));
        await waitFor(()=>expect(hook.result.current?.currentFiles?.[0].content).toBe('after'));
        expect(hook.result.current?.baseFiles).toEqual({'a.txt':'before'});
        expect('content' in base.files[0]).toBe(false);expect('content' in right.files[0]).toBe(false);
    });
    it('shares immutable bodies between two simultaneous diffs',async()=>{
        fetchMock.mockResolvedValue(response('shared','shared'));
        const right=setup(cell('base',[vf('a.txt','shared')]));
        const one=renderHook(()=>useCellDiff(right));const two=renderHook(()=>useCellDiff(right));
        await waitFor(()=>expect(one.result.current?.baseFiles).toEqual({'a.txt':'shared'}));
        await waitFor(()=>expect(two.result.current?.baseFiles).toEqual({'a.txt':'shared'}));
        expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    it('completes a new revision while an obsolete request remains pending',async()=>{
        let oldResolve:(value:unknown)=>void=()=>{};
        fetchMock.mockImplementationOnce(()=>new Promise(resolve=>{oldResolve=resolve;}));
        fetchMock.mockResolvedValue(response('new','new',2));
        const base=cell('base',[vf('a.txt','old')]);const right=setup(base);
        const hook=renderHook(()=>useCellDiff(right));
        await waitFor(()=>expect(fetchMock).toHaveBeenCalledTimes(1));
        act(()=>useCollimateStore.setState({cellCache:{base:{...base,files:[vf('a.txt','new',2)]}}}));
        await waitFor(()=>expect(hook.result.current?.baseFiles).toEqual({'a.txt':'new'}));
        await act(async()=>oldResolve(response('obsolete','old')));
        expect(hook.result.current?.baseFiles).toEqual({'a.txt':'new'});
    });
    it('holds more bodies than the shared accelerator can retain',async()=>{
        fetchMock.mockImplementation((url:string)=>{const name=new URL(url,'http://local').pathname.split('/').pop()!;return Promise.resolve(response(name,name));});
        const right=setup(cell('base',Array.from({length:70},(_,i)=>vf(`f${i}.txt`,`f${i}.txt`))));
        const hook=renderHook(()=>useCellDiff(right));
        await waitFor(()=>expect(Object.keys(hook.result.current?.baseFiles??{})).toHaveLength(70));
        expect(fetchMock).toHaveBeenCalledTimes(70);
    });
    it('does not show a body returned for the wrong revision',async()=>{
        fetchMock.mockResolvedValue(response('wrong','same',2));
        const right=setup(cell('base',[vf('a.txt','same',1)]));
        const hook=renderHook(()=>useCellDiff(right));
        await act(async()=>{await new Promise(resolve=>setTimeout(resolve,20));});
        expect(hook.result.current?.baseFiles).toBeNull();
    });
});
