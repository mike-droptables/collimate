import { useEffect, useState } from 'react';
import type { FileBody, VirtualFile } from '@/lib/types';
import { hydrateFiles } from '@/lib/fileCommands';
/** Explicit immutable body delivery for UI readers; metadata remains in the store. */
export function useFileBodies(channel:string,cellId:string,files:readonly VirtualFile[],versionId?:string,enabled=true){
    const signature=JSON.stringify([channel,cellId,versionId,files.map(f=>[f.name,f.file_revision,f.body_hash])]);
    const [loaded,setLoaded]=useState<{signature:string;files:FileBody[]} | null>(null);
    const [error,setError]=useState<string|null>(null);
    useEffect(()=>{
        if(!enabled)return;let active=true;setError(null);const controller=new AbortController();
        void hydrateFiles(channel,cellId,files,versionId,{signal:controller.signal}).then(bodies=>{if(active)setLoaded({signature,files:bodies});},e=>{if(active)setError(String(e?.message??e));});
        return()=>{active=false;controller.abort();};
    },[signature,enabled]);
    return {files:loaded?.signature===signature?loaded.files:null,error};
}
