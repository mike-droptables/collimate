import {useEffect,useState} from 'react';
import {requestJson} from '@/lib/api/client';
import {cellDocuments,subscribeDocuments,acquireFileDocument} from '@/lib/fileDocuments';
import {useCollimateStore} from '@/stores/useCollimateStore';
import type {FileConflict,FileDescriptor} from '@/lib/fileProtocol';
/** Server candidates remain visible even when the document's last view is closed. */
export function useCellFileConflicts(channel:string,cellId:string,enabled=true){
    const [conflicts,setConflicts]=useState<FileConflict[]>([]);
    useEffect(()=>{
        if(!enabled||!cellId)return;let active=true;let server:FileConflict[]=[];
        const refresh=()=>{if(!active)return;const all=new Map(server.map(c=>[c.conflict_id,c]));for(const doc of cellDocuments(channel,cellId))for(const c of doc.getSnapshot().conflicts)all.set(c.conflict_id,c);setConflicts([...all.values()]);};
        const load=()=>void requestJson(`/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/files/conflicts`,'GET').then(async(result:{conflicts:FileConflict[]})=>{
            if(!active)return;server=result.conflicts;refresh();
            const state=useCollimateStore.getState();const current=(state.cellCache[cellId]??state.locateCell(cellId)?.cell)?.files??[];
            const missing=server.filter(conflict=>!current.some(file=>file.name===conflict.path));
            if(!missing.length)return;
            const inventory=await requestJson(`/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/files`,'GET') as {files:FileDescriptor[]};
            for(const conflict of missing){if(!active)return;const file=inventory.files.find(file=>file.path===conflict.path);if(!file)continue;const {path,...metadata}=file;const owner=await acquireFileDocument(channel,cellId,{name:path,...metadata});owner.noteConflicts([conflict],file);}
        }).catch(()=>undefined);
        const onChange=(event:Event)=>{if((event as CustomEvent).detail?.cellId===cellId)load();};
        load();const unsubscribe=subscribeDocuments(refresh);window.addEventListener('collimate:files-changed-broadcast',onChange);
        return()=>{active=false;unsubscribe();window.removeEventListener('collimate:files-changed-broadcast',onChange);};
    },[channel,cellId,enabled]);
    return conflicts;
}
