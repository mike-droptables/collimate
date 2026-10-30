import {useSyncExternalStore} from 'react';
import {useCellFileConflicts} from '@/hooks/useCellFileConflicts';
import {cellDocuments,subscribeDocuments} from '@/lib/fileDocuments';
export function FileConflictNotice({channel,cellId,onOpenFile}:{channel:string;cellId:string;onOpenFile:(path:string)=>void}){
    const candidates=useCellFileConflicts(channel,cellId);
    const dirty=useSyncExternalStore(subscribeDocuments,()=>cellDocuments(channel,cellId).filter(doc=>doc.getSnapshot().competing||doc.getSnapshot().conflicts.length).map(doc=>doc.identity.path).sort().join('\u0000'));
    const paths=[...new Set([...candidates.map(c=>c.path),...dirty.split('\u0000').filter(Boolean)])];
    if(!paths.length)return null;
    return <div role="status" className="p-2 text-[0.75rem] border-b border-line">{paths.map(path=><button key={path} className="block text-red" onClick={()=>onOpenFile(path)}>Conflict · {path} · Review versions</button>)}</div>;
}
