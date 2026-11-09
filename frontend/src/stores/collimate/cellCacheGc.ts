import type {AppState,Cell,RunRecord} from '@/lib/types';
import {hasDirtyDocuments} from '@/lib/fileDocuments';
export const CELL_CACHE_GRACE_MS=60_000;
type CacheState={state:AppState|null;cellCache:Record<string,Cell>;runs:Record<string,RunRecord>};

/** One expiry timer, with an absence grace period for transient layout pushes. */
export function createCellCacheGc(read:()=>CacheState,write:(cache:Record<string,Cell>)=>void):()=>void {
    const absentSince=new Map<string,number>();
    let timer:ReturnType<typeof setTimeout>|undefined;
    const prune=()=>{
        if(timer!==undefined)clearTimeout(timer);
        timer=undefined;
        const state=read(),now=Date.now();
        const live=new Set(state.state?.channels.flatMap(channel=>[...channel.nav_stacks,...channel.main_stacks].flatMap(stack=>stack.cells.map(cell=>cell.id)))??[]);
        for(const run of Object.values(state.runs))live.add(run.cellId);
        for(const id of absentSince.keys())if(!state.cellCache[id]||live.has(id)||hasDirtyDocuments(id))absentSince.delete(id);
        let next=state.cellCache;
        for(const id of Object.keys(state.cellCache)){
            if(live.has(id)||hasDirtyDocuments(id))continue;
            const since=absentSince.get(id)??now;absentSince.set(id,since);
            if(now-since<CELL_CACHE_GRACE_MS)continue;
            if(next===state.cellCache)next={...next};delete next[id];absentSince.delete(id);
        }
        if(next!==state.cellCache)write(next);
        if(absentSince.size){const due=Math.min(...absentSince.values())+CELL_CACHE_GRACE_MS;timer=setTimeout(prune,Math.max(1,due-Date.now()));}
    };
    return prune;
}
