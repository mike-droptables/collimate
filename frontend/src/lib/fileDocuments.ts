import {isRunPath} from './runningCell';
import type {RendererRecoverySnapshot} from './fileSnapshotCodec';
import { FileDocument, documentKey, pendingDocumentRecord, type DocumentIdentity, type DocumentViewEdit } from './fileDocument';
import { documentJournal } from './fileDocumentStorage';
import { descriptorOf, readPinnedText } from './fileBodies';
import { getApiUrl, requestJson } from './api/client';
import type { VirtualFile } from './types';
import type { FileConflict, FileDescriptor, FileMutationResult, FileOperation } from './fileProtocol';
import { applyMutationResult } from './fileCommands';
import { useCollimateStore } from '@/stores/useCollimateStore';

const documents=new Map<string,FileDocument>();
const listeners=new Set<()=>void>();
let windowId: string | null=null;
/** A tab keeps its window namespace across reload; a duplicate gets a new one. */
async function windowNamespace(): Promise<string> {
    if(windowId)return windowId;
    let candidate=sessionStorage.getItem('collimate.documentWriter')??crypto.randomUUID();
    if(typeof BroadcastChannel!=='undefined'){
        const channel=new BroadcastChannel('collimate-document-writers');
        const nonce=crypto.randomUUID();let collision=false;
        channel.onmessage=event=>{
            const data=event.data;
            if(data?.writer!==candidate || data.nonce===nonce)return;
            if(data.type==='claim')channel.postMessage({type:'occupied',writer:candidate,nonce});
            if(data.type==='occupied')collision=true;
        };
        channel.postMessage({type:'claim',writer:candidate,nonce});
        await new Promise(resolve=>setTimeout(resolve,50));
        if(collision)candidate=crypto.randomUUID();
        // Keep answering claims while this window owns its writer.
        channel.onmessage=event=>{if(event.data?.type==='claim'&&event.data.writer===candidate)channel.postMessage({type:'occupied',writer:candidate,nonce});};
    }
    sessionStorage.setItem('collimate.documentWriter',candidate);windowId=candidate;return candidate;
}
let namespacePromise: Promise<string> | null=null;
function origin(): string {return new URL(getApiUrl(),window.location.href).origin;}
export const fileIdentity=(channel:string,cellId:string,path:string):DocumentIdentity=>({origin:origin(),channel,cellId,path});
export const mutateFiles=(channel:string,cellId:string,operation:FileOperation):Promise<FileMutationResult>=>requestJson(`/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/files/mutate`,'POST',operation);
export async function acquireFileDocument(channel:string,cellId:string,file:VirtualFile):Promise<FileDocument> {
    const identity=fileIdentity(channel,cellId,file.name);const key=documentKey(identity);
    let document=documents.get(key);
    if(!document){
        namespacePromise??=windowNamespace();
        const windowId=await namespacePromise;
        const records=await documentJournal.load(identity);
        const pending=records.filter(record=>record.writerId.startsWith(`${windowId}:`) && pendingDocumentRecord(record));
        // The window namespace identifies recovery ownership. Each fresh
        // editing session gets its own opaque sequence domain, independent of
        // paths and current server provenance. Pending recovery retains its
        // exact prior domain and receipt rather than allocating a new one.
        const writerId=pending.length===1?pending[0].writerId:`${windowId}:${crypto.randomUUID()}`;
        document=documents.get(key);
        if(!document){
            document=new FileDocument(identity,writerId,documentJournal,operation=>mutateFiles(channel,cellId,operation).then(result=>{applyMutationResult(cellId,result);return result;}),descriptor=>readPinnedText(channel,cellId,descriptor));
            documents.set(key,document);document.subscribe(()=>{for(const listener of listeners)listener();});
        }
    }
    await document.initialize(descriptorOf(file));
    await document.observe(descriptorOf(file));
    const queried=document.getSnapshot();
    void requestJson(`/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/files/conflicts`,'GET').then((result:{conflicts:FileConflict[]})=>{const now=document!.getSnapshot();if(now.savedSeq===queried.savedSeq&&now.baseRevision===queried.baseRevision)document!.noteConflicts(result.conflicts);}).catch(()=>undefined);
    return document;
}
export function getFileDocument(channel:string,cellId:string,path:string):FileDocument|undefined{return documents.get(documentKey(fileIdentity(channel,cellId,path)));}
export async function retargetFileDocument(channel:string,cellId:string,source:string,target:FileDescriptor,capturedEditSeq:number):Promise<boolean>{
    const key=documentKey(fileIdentity(channel,cellId,source)),owner=documents.get(key);
    if(!owner)return false;
    await owner.moveTo(target,capturedEditSeq);documents.delete(key);documents.set(documentKey(owner.identity),owner);
    for(const listener of listeners)listener();return true;
}
export const subscribeDocuments=(listener:()=>void):(()=>void)=>{listeners.add(listener);return ()=>{listeners.delete(listener);};};
export function cellDocuments(channel?:string,cellId?:string):FileDocument[]{return [...documents.values()].filter(doc=>(!channel||doc.identity.channel===channel)&&(!cellId||doc.identity.cellId===cellId));}
export async function flushFileDocuments(channel?:string,cellId?:string):Promise<void>{await Promise.all(cellDocuments(channel,cellId).map(doc=>doc.flush()));}
export async function ensureLocalFileDocuments(channel?:string,cellId?:string):Promise<void>{await Promise.all(cellDocuments(channel,cellId).map(doc=>doc.locallyDurable()));}
export async function editCellDocument(channel:string,cellId:string,path:string,body:string,view?:DocumentViewEdit,isCurrent?:()=>boolean):Promise<void>{
    if(isRunPath(path))throw new Error('Run-owned files are read-only.');
    const cell=useCollimateStore.getState().cellCache[cellId]??useCollimateStore.getState().locateCell(cellId)?.cell;
    const file=cell?.files.find(f=>f.name===path);
    if(!file)throw new Error(`File ${path} no longer exists.`);
    const document=await acquireFileDocument(channel,cellId,file);
    if(view&&!isCurrent?.())return;
    document.edit(body,false,view);
}
export async function snapshotCellDocument(channel:string,cellId:string,path:string,recovery:RendererRecoverySnapshot,view?:DocumentViewEdit,isCurrent?:()=>boolean):Promise<void>{
    if(isRunPath(path))throw new Error('Run-owned files are read-only.');
    const state=useCollimateStore.getState(),file=(state.cellCache[cellId]??state.locateCell(cellId)?.cell)?.files.find(file=>file.name===path);
    if(!file)throw new Error(`File ${path} no longer exists.`);
    const document=await acquireFileDocument(channel,cellId,file);
    if(view&&!isCurrent?.())return;
    document.editSnapshot(recovery,view);
}
export function observeCellDocuments(channel:string,cellId:string,files:VirtualFile[]):void {
    for(const file of files){const doc=getFileDocument(channel,cellId,file.name);if(doc)void doc.observe(descriptorOf(file)).catch(()=>undefined);}
}

export function hasDirtyDocuments(cellId:string):boolean{return cellDocuments(undefined,cellId).some(doc=>{const snapshot=doc.getSnapshot();return snapshot.editSeq>snapshot.savedSeq||!!snapshot.structuralPending||!!snapshot.structuralUncertain;});}

export function retainedFileMetadata(channel:string,cellId:string):VirtualFile[]{
    return cellDocuments(channel,cellId).filter(doc=>{const snapshot=doc.getSnapshot();return snapshot.editSeq>snapshot.savedSeq||snapshot.status==='Conflict'||snapshot.structuralPending||snapshot.structuralUncertain;}).flatMap(doc=>{
        const file=doc.getDescriptor();if(!file)return [];const {path,...meta}=file;return [{name:path,...meta}];
    });
}

export function forgetCellDocumentView(channel:string,cellId:string,path:string,viewId:string):void {getFileDocument(channel,cellId,path)?.forgetView(viewId);}
