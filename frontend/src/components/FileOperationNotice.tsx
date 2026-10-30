import {useEffect,useState} from 'react';
import {operationJournal,type PendingFileOperation} from '@/lib/fileDocumentStorage';
import {recoverFileOperations} from '@/lib/fileCommands';
import {getApiUrl} from '@/lib/api/client';
import {btn} from './ui/formStyles';

/** Exact persisted operations remain retryable after a lost server receipt. */
export function FileOperationNotice({channel,cellId}:{channel:string;cellId:string}){
    const [pending,setPending]=useState<PendingFileOperation[]>([]);
    const [busy,setBusy]=useState(false),[error,setError]=useState('');
    useEffect(()=>{
        let active=true,generation=0;
        const refresh=()=>{
            const request=++generation;
            void operationJournal.load().then(records=>{
                if(!active||request!==generation)return;
                const origin=new URL(getApiUrl(),window.location.href).origin;
                setPending(records.filter(record=>record.origin===origin&&record.channel===channel&&(record.cellId===cellId||record.move?.target_cell_id===cellId)));
            }).catch(()=>undefined);
        };
        refresh();const unsubscribe=operationJournal.subscribe(refresh);
        return()=>{active=false;unsubscribe();};
    },[channel,cellId]);
    if(!pending.length)return null;
    const retry=async()=>{
        setBusy(true);setError('');
        try{await recoverFileOperations(channel,cellId);}catch(error){setError(String((error as Error)?.message??error));}
        finally{setBusy(false);}
    };
    return <div role="status" className="px-2 py-1 text-[0.75rem] border-b border-line bg-sheet">
        File operation awaiting a server receipt. Its exact request is saved in this browser.{' '}
        <button className={btn} disabled={busy} onClick={()=>void retry()}>{busy?'Retrying…':'Retry pending operation'}</button>
        {error&&<p role="alert">{error}</p>}
    </div>;
}
