import { useEffect, useState, useSyncExternalStore } from 'react';
import { acquireFileDocument } from '@/lib/fileDocuments';
import type { FileDocument } from '@/lib/fileDocument';
import type { VirtualFile } from '@/lib/types';
const emptySubscribe=()=>()=>{};
const loading={body:null,editSeq:0,localSeq:0,savedSeq:0,baseRevision:0,status:'Loading' as const,error:null,competing:null,conflicts:[],recoveries:[]};
const emptySnapshot=()=>loading;
export function useFileDocument(channel:string,cellId:string,file:VirtualFile,enabled=true){
    const [document,setDocument]=useState<FileDocument|null>(null);
    const [error,setError]=useState<string|null>(null);
    useEffect(()=>{
        let active=true;if(!enabled){setDocument(null);return;}
        void acquireFileDocument(channel,cellId,file).then(doc=>{if(active){setDocument(doc);setError(null);}},e=>{if(active)setError(String(e?.message??e));});
        return ()=>{active=false;};
    },[channel,cellId,file.name,file.file_revision,file.body_hash,enabled]);
    const owned=document && document.identity.channel===channel && document.identity.cellId===cellId && document.identity.path===file.name?document:null;
    const snapshot=useSyncExternalStore(owned?.subscribe??emptySubscribe,owned?.getSnapshot??emptySnapshot);
    return {document:owned,snapshot,error};
}
