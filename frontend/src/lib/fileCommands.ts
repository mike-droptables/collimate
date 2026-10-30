import {isRunPath} from './runningCell';
import {operationJournal,documentJournal,type PendingFileOperation} from './fileDocumentStorage';
import {renameFileTab} from './cellLayout';
import {getApiUrl,requestJson} from './api/client';
import type { FileBody, VirtualFile } from './types';
import type { FileMutation, FileMutationResult } from './fileProtocol';
import { descriptorOf, readPinnedText } from './fileBodies';
import { mutateFiles, flushFileDocuments, getFileDocument,retargetFileDocument,acquireFileDocument,fileIdentity } from './fileDocuments';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { cellFileUrl } from './api/cells';
import type {FileRename} from './fileTransfer';

export const textFile=(name:string,content:string,language='plaintext'):FileBody=>({name,content,language,file_revision:0,body_hash:null,encoding:'utf-8',writer_id:'',edit_seq:0});
export const binaryFile=(name:string,body_hash:string,language='binary'):VirtualFile=>({name,language,file_revision:0,body_hash,encoding:'binary',writer_id:'',edit_seq:0});
export const withBody=(file:VirtualFile,content:string):FileBody=>({...file,content});
class UncertainFileMutation extends Error {constructor(error:unknown){super(String((error as Error)?.message??error));}}
export function currentCellFiles(cellId:string):VirtualFile[] {
    const state=useCollimateStore.getState();const cell=state.cellCache[cellId]??state.locateCell(cellId)?.cell;
    if(!cell)throw new Error('This cell is no longer available.');return cell.files;
}
export async function readCellText(channel:string,cellId:string,path:string,versionId?:string,init?:RequestInit):Promise<string> {
    if(versionId){const response=await fetch(cellFileUrl(channel,cellId,path,versionId),init);if(!response.ok)throw Object.assign(new Error(`HTTP ${response.status}`),{status:response.status});return response.text();}
    const file=currentCellFiles(cellId).find(file=>file.name===path);
    if(!file)throw Object.assign(new Error(`File ${path} does not exist.`),{status:404});
    return readPinnedText(channel,cellId,descriptorOf(file),init);
}
/** Renderer sidecars share the document owner and expose recoverable local edits. */
export async function readRendererSiblingText(channel:string,cellId:string,path:string,versionId?:string):Promise<string>{
    if(versionId)return readCellText(channel,cellId,path,versionId);
    let owner=getFileDocument(channel,cellId,path);
    if(!owner){
        const file=currentCellFiles(cellId).find(file=>file.name===path);
        if(file?.encoding==='binary')throw new Error(`File ${path} contains binary bytes.`);
        if(file)owner=await acquireFileDocument(channel,cellId,file);
        else if((await documentJournal.load(fileIdentity(channel,cellId,path))).length)owner=await acquireFileDocument(channel,cellId,{...textFile(path,''),file_revision:await expectedFileRevision(channel,cellId,path),deleted:true});
    }
    return owner?owner.materializedBody():readCellText(channel,cellId,path);
}
export async function hydrateFiles(channel:string,cellId:string,files:readonly VirtualFile[],versionId?:string,init?:RequestInit):Promise<FileBody[]> {
    return Promise.all(files.map(async file=>withBody(file,file.encoding==='binary'?'':('content' in file?String(file.content):await (versionId?readCellText(channel,cellId,file.name,versionId,init):readPinnedText(channel,cellId,descriptorOf(file),init))))));
}
export function applyMutationResult(cellId:string,result:FileMutationResult):void {
    if(result.outcome!=='accepted')return;
    const state=useCollimateStore.getState();const cell=state.cellCache[cellId]??state.locateCell(cellId)?.cell;
    if(!cell)return;
    const existing=cell.files;const revisions=new Map(existing.map(file=>[file.name,file.file_revision]));
    const updates=new Map(result.files.filter(file=>file.file_revision>(revisions.get(file.path)??-1)).map(file=>[file.path,file]));
    const files=existing.filter(file=>!updates.get(file.name)?.deleted).map(file=>{
        const update=updates.get(file.name);if(!update)return file;const {path,...metadata}=update;updates.delete(path);return {name:path,...metadata};
    });
    for(const [path,file] of updates){if(file.deleted)continue;const {path:_,...metadata}=file;files.push({name:path,...metadata});}
    state.patchCell(cellId,{files});
}
export async function submitFileMutations(channel:string,cellId:string,mutations:FileMutation[],resolves:string[]=[],renames?:PendingFileOperation['renames'],interaction=false):Promise<FileMutationResult> {
    if(!interaction&&mutations.some(mutation=>isRunPath(mutation.path)))throw new Error('Run-owned files are read-only.');
    if(!mutations.length)return {operation_id:'',outcome:'accepted',files:[],conflicts:[]};
    const operation={operation_id:crypto.randomUUID(),mutations,resolves};
    const record={key:operation.operation_id,origin:new URL(getApiUrl(),window.location.href).origin,channel,cellId,operation,renames};
    await operationJournal.save(record);
    let result:FileMutationResult;
    try{result=await mutateFiles(channel,cellId,operation);}catch(error){throw new UncertainFileMutation(error);}
    try{validateMutationReceipt(operation,result);}catch(error){throw new UncertainFileMutation(error);}
    applyMutationResult(cellId,result);
    if(result.outcome==='accepted')await reconcileRenames(record,result);
    await operationJournal.remove(record.key);
    if(result.outcome!=='accepted')throw new Error('File conflict: the competing version was preserved. Resolve it before continuing.');
    return result;
}
const commandWriter=()=>crypto.randomUUID();
/** Atomic conditional file transform. Unchanged metadata never travels as a write. */
export async function replaceCellFiles(channel:string,cellId:string,desired:readonly VirtualFile[],baseline:readonly VirtualFile[]=currentCellFiles(cellId),mappings:readonly FileRename[]=[]):Promise<void> {
    const oldNames=new Set(baseline.map(file=>file.name)),newNames=new Set(desired.map(file=>file.name));
    const renamed=mappings.flatMap(mapping=>{
        const source=baseline.find(file=>file.name===mapping.source),target=desired.find(file=>file.name===mapping.target);
        if(!source||!target||newNames.has(mapping.source)||oldNames.has(mapping.target))throw new Error('Rename source or destination changed.');
        const owner=target?getFileDocument(channel,cellId,source.name):undefined;
        return owner&&target?[{source,target,owner}]:[];
    });
    if(!mappings.length)await flushFileDocuments(channel,cellId);
    const barriers:typeof renamed=[];
    try {
    for(const item of renamed){const snapshot=await item.owner.beginStructural();barriers.push(item);if(snapshot.body!==null)desired=desired.map(file=>file.name===item.target.name?withBody(file,snapshot.body!):file);baseline=baseline.map(file=>file.name===item.source.name?{...file,file_revision:snapshot.baseRevision}:file);}
    const prior=new Map(baseline.map(file=>[file.name,file]));const after=new Map(desired.map(file=>[file.name,file]));const mutations:FileMutation[]=[];
    const missing=desired.filter(file=>!prior.has(file.name));
    const inventory=missing.length?await requestJson(`/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/files`,'GET') as {files:import('./fileProtocol').FileDescriptor[]}:null;
    const tombstones=new Map((inventory?.files??[]).filter(file=>file.deleted).map(file=>[file.path,file.file_revision]));
    const writer=commandWriter();let seq=0;
    for(const file of desired){
        const old=prior.get(file.name);const body='content' in file?String(file.content):undefined;
        if(body===undefined && old?.body_hash===file.body_hash && old?.encoding===file.encoding && old?.language===file.language)continue;
        if(body===undefined && !file.body_hash)throw new Error(`File ${file.name} has no admitted body.`);
        mutations.push({path:file.name,writer_id:writer,edit_seq:++seq,base_revision:old?.file_revision??tombstones.get(file.name)??0,kind:'put',language:file.language,encoding:file.encoding,...(body!==undefined&&file.encoding!=='binary'?{content_text:body}:{blob_hash:file.body_hash!})});
    }
    for(const file of baseline)if(!after.has(file.name))mutations.push({path:file.name,writer_id:writer,edit_seq:++seq,base_revision:file.file_revision,kind:'delete'});
    await submitFileMutations(channel,cellId,mutations,[],barriers.map(item=>({source:item.source.name,target:item.target.name,writerId:item.owner.writerId,capturedEditSeq:item.owner.getSnapshot().editSeq})));
    }catch(error){for(const item of barriers)await item.owner.finishStructural(false,error instanceof UncertainFileMutation);throw error;}
}
export async function writeCellBlob(channel:string,cellId:string,path:string,hash:string):Promise<void>{
    const files=currentCellFiles(cellId);const existing=files.find(file=>file.name===path);
    await submitFileMutations(channel,cellId,[{path,writer_id:commandWriter(),edit_seq:1,base_revision:await expectedFileRevision(channel,cellId,path),kind:'put',blob_hash:hash,language:existing?.language??'binary',encoding:'binary'}]);
}
export async function expectedFileRevision(channel:string,cellId:string,path:string):Promise<number>{
    const current=currentCellFiles(cellId).find(file=>file.name===path);if(current)return current.file_revision;
    const inventory=await requestJson(`/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/files`,'GET') as {files:import('./fileProtocol').FileDescriptor[]};
    return inventory.files.find(file=>file.path===path&&file.deleted)?.file_revision??0;
}

export const needsTextBody=(file:VirtualFile):boolean=>file.encoding==='utf-8' && !('content' in file);

export async function recoverFileOperations(channel:string,cellId?:string):Promise<void>{
    const origin=new URL(getApiUrl(),window.location.href).origin;
    const records=await operationJournal.load();
    for(const record of records.filter(record=>record.origin===origin&&record.channel===channel&&(!cellId||record.cellId===cellId||record.move?.target_cell_id===cellId))){
        if(record.move){
            const result=await requestJson(`/channels/${encodeURIComponent(record.channel)}/files/move`,'POST',record.move) as FileMoveResult;
            validateMoveReceipt(record.move,result);
            applyMutationResult(record.move.source_cell_id,{...result,files:result.source_files});applyMutationResult(record.move.target_cell_id,{...result,files:result.target_files});
            await operationJournal.remove(record.key);if(result.outcome!=='accepted')throw new Error('A recovered move has a preserved conflict.');continue;
        }
        const result=await mutateFiles(record.channel,record.cellId,record.operation);
        validateMutationReceipt(record.operation,result);
        applyMutationResult(record.cellId,result);if(result.outcome==='accepted')await reconcileRenames(record,result);await operationJournal.remove(record.key);
        if(result.outcome!=='accepted')throw new Error('A recovered file operation has a preserved conflict.');
    }
}
function validateMutationReceipt(operation:import('./fileProtocol').FileOperation,result:FileMutationResult):void {
    if(result.operation_id!==operation.operation_id || !['accepted','conflict','superseded'].includes(result.outcome))throw new Error('Invalid persistence acknowledgement.');
    if(result.outcome==='accepted')for(const mutation of operation.mutations){
        const file=result.files.find(file=>file.path===mutation.path);
        if(!file||file.file_revision<=mutation.base_revision||file.writer_id!==mutation.writer_id||file.edit_seq!==mutation.edit_seq||file.deleted!==(mutation.kind==='delete'))throw new Error('Invalid persistence acknowledgement.');
    }
}
async function reconcileRenames(record:PendingFileOperation,result:FileMutationResult):Promise<void>{
    for(const rename of record.renames??[]){
        const file=result.files.find(file=>file.path===rename.target&&!file.deleted);if(!file)throw new Error('Rename acknowledgement lacks its destination.');
        const moved=await retargetFileDocument(record.channel,record.cellId,rename.source,file,rename.capturedEditSeq);
        if(!moved){
            const identity={origin:record.origin,channel:record.channel,cellId:record.cellId,path:rename.source};
            const local=(await documentJournal.load(identity)).find(local=>local.writerId===rename.writerId);
            if(local&&local.editSeq>rename.capturedEditSeq){await documentJournal.save({...local,identity:{...identity,path:rename.target},baseRevision:file.file_revision,savedSeq:rename.capturedEditSeq,operation:null,conflicts:[],structuralUncertain:false});}
            await documentJournal.remove(rename.writerId,identity);
        }
        useCollimateStore.getState().updateCellLayout(record.cellId,layout=>renameFileTab(layout,rename.source,rename.target));
    }
}
export interface MoveFile {source_path:string;target_path:string;source_revision:number;target_revision:number}
type FileMoveResult=FileMutationResult & {source_files:FileMutationResult['files'];target_files:FileMutationResult['files']};
function validateMoveReceipt(move:NonNullable<PendingFileOperation['move']>,result:FileMoveResult):void{
    const invalid=()=>new Error('Invalid move acknowledgement. The exact pending operation is preserved for retry.');
    if(result.operation_id!==move.operation_id||!['accepted','conflict','superseded'].includes(result.outcome)||!Array.isArray(result.source_files)||!Array.isArray(result.target_files))throw invalid();
    if(result.outcome!=='accepted')return;
    if(result.source_files.length!==move.files.length||result.target_files.length!==move.files.length)throw invalid();
    for(const expected of move.files){
        const source=result.source_files.find(file=>file.path===expected.source_path),target=result.target_files.find(file=>file.path===expected.target_path);
        if(!source||!target||source.file_revision<=expected.source_revision||target.file_revision<=expected.target_revision||source.deleted!==true||source.body_hash!==null||target.deleted!==false||!target.body_hash||source.writer_id!==move.writer_id||target.writer_id!==move.writer_id||source.edit_seq!==move.edit_seq||target.edit_seq!==move.edit_seq)throw invalid();
    }
}
export async function moveCellFiles(channel:string,sourceCellId:string,targetCellId:string,files:MoveFile[]):Promise<void>{
    const move={operation_id:crypto.randomUUID(),source_cell_id:sourceCellId,target_cell_id:targetCellId,files,writer_id:crypto.randomUUID(),edit_seq:1};
    const record={key:move.operation_id,origin:new URL(getApiUrl(),window.location.href).origin,channel,cellId:sourceCellId,operation:{operation_id:move.operation_id,mutations:[],resolves:[]},move};
    await operationJournal.save(record);
    const result=await requestJson(`/channels/${encodeURIComponent(channel)}/files/move`,'POST',move) as FileMoveResult;
    validateMoveReceipt(move,result);
    if(result.outcome==='accepted'){applyMutationResult(sourceCellId,{...result,files:result.source_files});applyMutationResult(targetCellId,{...result,files:result.target_files});}
    await operationJournal.remove(record.key);
    if(result.outcome!=='accepted')throw new Error('Move conflict: both cells keep their current versions.');
}
