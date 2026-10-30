import { useCollimateStore } from '@/stores/useCollimateStore';
import { expectedFileRevision,textFile } from './fileCommands';
import { acquireFileDocument } from './fileDocuments';
export type SiblingWriteReason='unavailable'|'refused'|'read-only';
export interface SiblingWriteResult {ok:boolean;reason?:SiblingWriteReason}
/** Sibling edits use the same conditional service during and outside runs. */
export async function saveSiblingFileInCell(cellId:string|undefined,path:string,content:string,readOnly?:boolean):Promise<SiblingWriteResult>{
    if(readOnly)return {ok:false,reason:'read-only'};
    if(!cellId||!path||typeof content!=='string')return {ok:false,reason:'unavailable'};
    const location=useCollimateStore.getState().locateCell(cellId);
    if(!location)return {ok:false,reason:'unavailable'};
    try{
        const state=useCollimateStore.getState();const file=(state.cellCache[cellId]??state.locateCell(cellId)?.cell)?.files.find(file=>file.name===path);
        if(file?.encoding==='binary')return {ok:false,reason:'refused'};
        const metadata=file??{...textFile(path,''),file_revision:await expectedFileRevision(location.channel,cellId,path),deleted:true};
        const owner=await acquireFileDocument(location.channel,cellId,metadata);
        // The owner journals first even if another pending save is refused.
        owner.edit(content,!file);await owner.flush();
        return {ok:true};
    }catch{return {ok:false,reason:'refused'};}
}
