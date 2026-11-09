import { useCallback, useEffect, useRef, useState } from 'react';
import type { Cell } from '@/lib/types';
import type { FullFileEditorHandle } from '../FullFileEditor';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useIdleCheckpoint } from '@/hooks/useIdleCheckpoint';
import { boundedFlush, flushRendererEditors } from '@/lib/rendererFlush';
import { registerCellSaveBarrier } from '@/lib/cellSaveBarrier';
import { cellDocuments, flushFileDocuments, ensureLocalFileDocuments, subscribeDocuments } from '@/lib/fileDocuments';
import { patchCellMeta } from '@/lib/api';
import { currentChannelId } from '@/lib/currentChannel';
interface CellSaveStateOptions {cell:Cell;channelId:string|undefined;readOnly:boolean;onUpdate:(cell:Cell)=>void;openPathsKey:string;onCheckpoint:()=>void}
/** Cell chrome observes document receipts; it never owns another file buffer. */
export function useCellSaveState({cell,channelId,readOnly,openPathsKey,onCheckpoint}:CellSaveStateOptions){
    const channel=channelId||currentChannelId();
    const [dirtyByName,setDirtyByName]=useState<Record<string,boolean>>({});
    const [lastSavedByName,setLastSavedByName]=useState<Record<string,Date>>({});
    const {notifyEdit,flushCheckpoint}=useIdleCheckpoint(channel,cell.id,!readOnly,onCheckpoint);
    const seen=useRef(new Map<string,{edit:number;saved:number}>());
    useEffect(()=>{
        const refresh=()=>{
            const dirty:Record<string,boolean>={};
            for(const document of cellDocuments(channel,cell.id)){
                const snapshot=document.getSnapshot();const path=document.identity.path;
                dirty[path]=snapshot.editSeq>snapshot.savedSeq || snapshot.status==='Conflict';
                const prev=seen.current.get(path);
                seen.current.set(path,{edit:snapshot.editSeq,saved:snapshot.savedSeq});
                if(snapshot.editSeq>(prev?.edit??0))notifyEdit();
                if(snapshot.savedSeq>(prev?.saved??0))setLastSavedByName(old=>({...old,[path]:new Date()}));
            }
            setDirtyByName(old=>JSON.stringify(old)===JSON.stringify(dirty)?old:dirty);
        };
        refresh();return subscribeDocuments(refresh);
    },[channel,cell.id,notifyEdit]);
    const latestCell=()=>{const cache=useCollimateStore.getState().cellCache[cell.id];return {...cell,...cache,files:cache?.files??cell.files};};
    const commitCell=async(patch:Partial<Cell>)=>{
        if(readOnly)return;
        if(patch.files)throw new Error('File changes require conditional document mutations.');
        const receipt=await patchCellMeta(channel,cell.id,{...(patch.name!==undefined?{name:patch.name}:{}),...('icon' in patch?{icon:patch.icon??null}:{}),...('pinned' in patch?{pinned:patch.pinned??null}:{}),...('settings' in patch?{settings:patch.settings}: {})});
        useCollimateStore.getState().patchCell(cell.id,receipt);
    };
    const editorHandlesRef=useRef(new Map<string,FullFileEditorHandle>());
    const registerEditorHandle=useCallback((tabId:string,handle:FullFileEditorHandle|null)=>{if(handle)editorHandlesRef.current.set(tabId,handle);else editorHandlesRef.current.delete(tabId);},[]);
    const saveNow=useCallback(async()=>{if(readOnly)return;await boundedFlush(async()=>{
        await flushRendererEditors(cell.id);
        await Promise.all([...editorHandlesRef.current.values()].map(handle=>handle.flushPendingSave()));
        await flushFileDocuments(channel,cell.id);
    });},[channel,cell.id,readOnly]);
    const prepareViewChange=useCallback(async()=>{if(readOnly)return;await boundedFlush(async()=>{
        await flushRendererEditors(cell.id);
        await ensureLocalFileDocuments(channel,cell.id);
    });},[channel,cell.id,readOnly]);
    useEffect(()=>readOnly?undefined:registerCellSaveBarrier(cell.id,saveNow),[cell.id,readOnly,saveNow]);
    const previousPaths=useRef<string|null>(null);
    useEffect(()=>{const previous=previousPaths.current;previousPaths.current=openPathsKey;if(previous!==null && previous.split('\u0000').some(path=>!openPathsKey.split('\u0000').includes(path)))flushCheckpoint();},[openPathsKey,flushCheckpoint]);
    return {dirtyByName,lastSavedByName,notifyEdit,latestCell,commitCell,registerEditorHandle,saveNow,prepareViewChange,onBeforeStructuralChange:prepareViewChange};
}
export type CellSaveState=ReturnType<typeof useCellSaveState>;
