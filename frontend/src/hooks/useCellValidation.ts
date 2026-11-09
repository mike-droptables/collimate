import {useEffect,useState} from 'react';
import {fetchCellValidation,type CellValidation} from '@/lib/definitionApi';
import {RENDERER_PREFS_CHANGED_EVENT} from '@/lib/api';
import type {VirtualFile} from '@/lib/types';

/** Server verdicts are advisory; editor saves never wait for this observer. */
export function useCellValidation(channel:string|undefined,cellId:string,files:VirtualFile[],visible:boolean,savedFiles:Readonly<Record<string,Date>>) {
    const [state,setState]=useState<{channel:string;cellId:string;files:VirtualFile[];savedFiles:Readonly<Record<string,Date>>;value:CellValidation}|null>(null);
    useEffect(()=>{
        if(!channel||!visible)return;
        let live=true,sequence=0;
        let timer:ReturnType<typeof setTimeout>|undefined;
        const refresh=async()=>{
            const request=++sequence;
            clearTimeout(timer);
            setState(null);
            try{
                const value=await fetchCellValidation(channel,cellId);
                if(!live||request!==sequence)return;
                setState({channel,cellId,files,savedFiles,value});
                if(value.pending)timer=setTimeout(refresh,500);
            }catch{
                // A temporary validation transport error must not present an
                // old verdict as current or interfere with writing the file.
                if(live&&request===sequence)timer=setTimeout(refresh,2000);
            }
        };
        const onRefresh=()=>{void refresh();};
        void refresh();
        window.addEventListener('focus',onRefresh);
        window.addEventListener(RENDERER_PREFS_CHANGED_EVENT,onRefresh);
        return()=>{live=false;++sequence;clearTimeout(timer);window.removeEventListener('focus',onRefresh);window.removeEventListener(RENDERER_PREFS_CHANGED_EVENT,onRefresh);};
    },[channel,cellId,files,visible,savedFiles]);
    return visible&&state&&state.channel===channel&&state.cellId===cellId&&state.files===files&&state.savedFiles===savedFiles?state.value:null;
}
