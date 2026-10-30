import type { FileConflict, FileDescriptor, FileMutationResult, FileOperation } from './fileProtocol';
import {materializeSnapshot,type RendererRecoverySnapshot,type RendererDocumentSnapshot} from './fileSnapshotCodec';

export interface DocumentViewEdit {viewId:string;viewSeq:number}
export type DocumentViewSnapshot=RendererDocumentSnapshot&{ownerId:string;viewAcks:Readonly<Record<string,number>>};
export interface DocumentIdentity { origin: string; channel: string; cellId: string; path: string }
export const documentKey = (identity: DocumentIdentity): string => JSON.stringify([identity.origin,identity.channel,identity.cellId,identity.path]);
export interface DocumentRecord {
    identity: DocumentIdentity; writerId: string; body: string; editSeq: number;
    baseRevision: number; savedSeq: number; localSeq: number;
    operation: FileOperation | null; conflicts: FileConflict[];
    recovery?:RendererRecoverySnapshot;materializedSeq?:number;structuralUncertain?:boolean;
}
export const pendingDocumentRecord = (record: DocumentRecord): boolean => !!(record.operation || record.editSeq > record.savedSeq || record.conflicts.length || record.structuralUncertain);
export interface DocumentJournal {
    load(identity: DocumentIdentity): Promise<DocumentRecord[]>;
    save(record: DocumentRecord): Promise<void>;
    remove(writerId: string, identity: DocumentIdentity): Promise<void>;
}
export type DocumentStatus = 'Loading' | 'Saving locally' | 'Saved locally · waiting for server' | 'Saving to server' | 'Saved' | 'Conflict' | 'Local save failed' | 'Server save failed' | 'Load failed';
export interface DocumentSnapshot {
    body: string | null; editSeq: number; localSeq: number; savedSeq: number; baseRevision: number;
    status: DocumentStatus; error: string | null; competing: FileDescriptor | null;
    conflicts: FileConflict[]; recoveries: DocumentRecord[];
    structuralPending?: boolean;
    structuralUncertain?:boolean;
    recovery?:RendererRecoverySnapshot;materializedSeq?:number;
    viewAcks?:Readonly<Record<string,number>>;
}
type Send = (operation: FileOperation) => Promise<FileMutationResult>;
type Read = (descriptor: FileDescriptor) => Promise<string>;
const bodyHash=async(body:string):Promise<string>=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(body)))).map(byte=>byte.toString(16).padStart(2,'0')).join('');

/** Lifetime belongs to the document, independently of any renderer/view lease. */
export class FileDocument {
    readonly instanceId=crypto.randomUUID();
    private snapshot: DocumentSnapshot = {body:null,editSeq:0,localSeq:0,savedSeq:0,baseRevision:0,status:'Loading',error:null,competing:null,conflicts:[],recoveries:[]};
    private listeners = new Set<()=>void>();
    private descriptor: FileDescriptor | null = null;
    private operation: FileOperation | null = null;
    private localTail: Promise<void> = Promise.resolve();
    private pendingDraftWrite: {write:{record:DocumentRecord}; task:Promise<void>} | null = null;
    private sending: Promise<void> | null = null;
    private timer: ReturnType<typeof setTimeout> | null = null;
    private generation = 0;
    private remoteDeadline: number | null=null;
    private initialized: Promise<void> | null = null;
    private materializing:Promise<void>|null=null;
    private resolving: string[] = [];
    constructor(readonly identity: DocumentIdentity, readonly writerId: string, private journal: DocumentJournal, private send: Send, private read: Read, private delay = 250,private derive=materializeSnapshot) {}
    getDescriptor = (): FileDescriptor|null => this.descriptor;
    getSnapshot = (): DocumentSnapshot => this.snapshot;
    getDocumentSnapshot=():DocumentViewSnapshot=>({ownerId:this.instanceId,viewAcks:this.snapshot.viewAcks??{},content:this.snapshot.body??'',recovery:this.snapshot.recovery,editSeq:this.snapshot.editSeq,materializedSeq:this.snapshot.materializedSeq??this.snapshot.editSeq});
    subscribe = (listener: ()=>void): (()=>void) => {this.listeners.add(listener);return ()=>{this.listeners.delete(listener);};};
    private update(patch: Partial<DocumentSnapshot>) { this.snapshot={...this.snapshot,...patch};for(const listener of this.listeners)listener(); }
    private record(): DocumentRecord {
        const s=this.snapshot;
        return {identity:{...this.identity},writerId:this.writerId,body:s.body??'',editSeq:s.editSeq,baseRevision:s.baseRevision,savedSeq:s.savedSeq,localSeq:s.localSeq,operation:this.operation,conflicts:s.conflicts,recovery:s.recovery,materializedSeq:s.materializedSeq,structuralUncertain:s.structuralUncertain};
    }
    private persist(coalesceDraft = false): Promise<void> {
        // Renderer snapshots are immutable owned values; IDB itself clones them.
        const record=this.record();
        const pending=this.pendingDraftWrite;
        if(coalesceDraft && pending && pending.write.record.operation===record.operation
            && pending.write.record.writerId===record.writerId
            && documentKey(pending.write.record.identity)===documentKey(record.identity)) {
            // Replace only a draft write that has not started. Its
            // waiters share the commit of this newer, subsuming sequence.
            pending.write.record=record;
            return pending.task;
        }
        // Materialization, operation receipts, and lifecycle writes are
        // ordering barriers. A draft must never replace across one.
        this.pendingDraftWrite=null;
        const write={record};
        const task=this.localTail.catch(()=>undefined).then(()=>{
            if(this.pendingDraftWrite?.write===write)this.pendingDraftWrite=null;
            return this.journal.save(write.record);
        }).then(()=>{
            if(write.record.editSeq>this.snapshot.localSeq)this.update({localSeq:write.record.editSeq});
            if(this.snapshot.localSeq===this.snapshot.editSeq && this.snapshot.status==='Saving locally')this.update({status:this.snapshot.competing||this.snapshot.conflicts.length?'Conflict':'Saved locally · waiting for server',error:null});
        },error=>{this.update({status:'Local save failed',error:String(error?.message??error)});throw error;});
        this.localTail=task;
        if(coalesceDraft)this.pendingDraftWrite={write,task};
        // Edits are synchronous; subscribers see an error even if no caller awaits.
        void task.catch(()=>undefined);
        return task;
    }
    initialize(descriptor: FileDescriptor): Promise<void> {
        if(this.initialized)return this.initialized;
        this.descriptor=descriptor;
        const started=this.generation;
        this.initialized=(async()=>{
            const records=await this.journal.load(this.identity);
            const own=records.find(record=>record.writerId===this.writerId);
            this.update({recoveries:records.filter(record=>record.writerId!==this.writerId && pendingDocumentRecord(record))});
            if(own && pendingDocumentRecord(own) && started===this.generation){
                this.operation=own.operation;
                this.update({body:own.body,recovery:own.recovery,materializedSeq:own.materializedSeq??(own.recovery?0:own.editSeq),editSeq:own.editSeq,localSeq:own.editSeq,savedSeq:own.savedSeq,baseRevision:own.baseRevision,conflicts:own.conflicts,structuralUncertain:own.structuralUncertain,status:own.conflicts.length?'Conflict':own.structuralUncertain?'Server save failed':own.editSeq>own.savedSeq?'Saved locally · waiting for server':'Saved'});
                if(descriptor.file_revision>own.baseRevision && descriptor.writer_id!==this.writerId)this.update({competing:descriptor,status:'Conflict'});
                if(!this.snapshot.competing && !own.conflicts.length && (own.operation||own.editSeq>own.savedSeq))void this.materializedBody().then(()=>this.schedule(),()=>undefined);
                return;
            }
            const body=descriptor.deleted?'':await this.read(descriptor);
            if(started!==this.generation)return;
            this.update({body,materializedSeq:this.snapshot.editSeq,baseRevision:descriptor.file_revision,status:'Saved'});
        })().catch(error=>{this.update({status:'Load failed',error:String(error?.message??error)});throw error;});
        return this.initialized;
    }
    forgetView(viewId:string):void {
        if(!this.snapshot.viewAcks?.[viewId])return;
        const viewAcks={...this.snapshot.viewAcks};delete viewAcks[viewId];this.update({viewAcks});
    }
    private acknowledgeView(view?:DocumentViewEdit):Readonly<Record<string,number>>|null {
        if(!view)return this.snapshot.viewAcks??{};
        if(!view.viewId||!Number.isSafeInteger(view.viewSeq)||view.viewSeq<1||view.viewSeq<=(this.snapshot.viewAcks?.[view.viewId]??0))return null;
        return {...this.snapshot.viewAcks,[view.viewId]:view.viewSeq};
    }
    edit(body: string,force=false,view?:DocumentViewEdit): void {
        if(this.snapshot.structuralPending)return;
        const viewAcks=this.acknowledgeView(view);if(!viewAcks)return;
        if(body===this.snapshot.body&&!force){if(view)this.update({viewAcks});return;}
        this.generation++;
        this.update({body,viewAcks,recovery:undefined,materializedSeq:this.snapshot.editSeq+1,editSeq:this.snapshot.editSeq+1,baseRevision:this.snapshot.baseRevision||this.descriptor?.file_revision||0,status:'Saving locally',error:null});
        const durability=this.persist(true);
        void durability.then(()=>this.schedule(),()=>undefined);
    }
    editSnapshot(recovery:RendererRecoverySnapshot,view?:DocumentViewEdit):void {
        if(this.snapshot.structuralPending)return;
        const viewAcks=this.acknowledgeView(view);if(!viewAcks)return;
        if(recovery.snapshotId===this.snapshot.recovery?.snapshotId){if(view)this.update({viewAcks});return;}
        this.generation++;
        this.update({recovery,viewAcks,editSeq:this.snapshot.editSeq+1,baseRevision:this.snapshot.baseRevision||this.descriptor?.file_revision||0,status:'Saving locally',error:null});
        void this.persist(true).then(()=>this.materializedBody()).then(()=>this.schedule()).catch(()=>undefined);
    }
    async materializedBody():Promise<string>{
        if(this.initialized)await this.initialized;
        if(this.materializing){await this.materializing;return this.materializedBody();}
        if(!this.snapshot.recovery||(this.snapshot.materializedSeq??0)>=this.snapshot.editSeq)return this.snapshot.body??'';
        const pending=(async()=>{
            while(this.snapshot.recovery&&(this.snapshot.materializedSeq??0)<this.snapshot.editSeq){
                await this.localTail;
                const recovery=this.snapshot.recovery,seq=this.snapshot.editSeq;
                const body=await this.derive(recovery);
                if(!this.snapshot.recovery||seq<=(this.snapshot.materializedSeq??0))continue;
                // A completed older snapshot remains an exact useful checkpoint.
                // Keep the latest raw recovery and dirty sequence beside its bytes.
                this.update({body,materializedSeq:seq});await this.persist();this.schedule();
            }
        })();this.materializing=pending;
        try{await pending;}catch(error){this.update({status:'Local save failed',error:`Snapshot materialization failed: ${String((error as Error)?.message??error)}`});throw error;}finally{if(this.materializing===pending)this.materializing=null;}
        return this.snapshot.body??'';
    }
    private schedule() {
        if(this.snapshot.structuralPending ||this.snapshot.structuralUncertain|| this.snapshot.competing || this.snapshot.conflicts.length)return;
        if(this.timer)clearTimeout(this.timer);
        this.remoteDeadline??=Date.now()+1000;
        this.timer=setTimeout(()=>{this.timer=null;this.remoteDeadline=null;void this.saveCheckpoint().catch(()=>undefined);},Math.min(this.delay,Math.max(0,this.remoteDeadline-Date.now())));
    }
    async observe(descriptor: FileDescriptor): Promise<void> {
        if(descriptor.path!==this.identity.path || descriptor.file_revision<=(this.descriptor?.file_revision??0))return;
        this.descriptor=descriptor;
        if(this.snapshot.structuralPending||this.snapshot.structuralUncertain)return;
        if(this.snapshot.editSeq>this.snapshot.savedSeq || this.operation){
            if(descriptor.writer_id===this.writerId && (descriptor.edit_seq<=this.snapshot.savedSeq || descriptor.edit_seq===this.operation?.mutations.find(mutation=>mutation.path===descriptor.path)?.edit_seq))return;
            this.update({competing:descriptor,status:'Conflict'});
            return;
        }
        const generation=++this.generation;
        const body=descriptor.deleted?'':await this.read(descriptor);
        if(generation!==this.generation || this.snapshot.editSeq>this.snapshot.savedSeq)return;
        this.update({body,recovery:undefined,materializedSeq:this.snapshot.editSeq,baseRevision:descriptor.file_revision,status:'Saved',error:null});
    }
    async flush(): Promise<void> {
        if(this.snapshot.structuralPending)throw new Error('A file operation is in progress.');
        if(this.snapshot.structuralUncertain)throw new Error('Retry the pending file operation before saving this document.');
        if(this.timer){clearTimeout(this.timer);this.timer=null;}this.remoteDeadline=null;
        if(this.initialized)await this.initialized;
        await this.localTail;
        await this.materializedBody();await this.localTail;
        if(this.sending){await this.sending;return this.flush();}
        if((this.snapshot.competing || this.snapshot.conflicts.length) && !this.resolving.length)throw new Error('File conflict needs explicit resolution.');
        if(!this.operation && this.snapshot.editSeq<=this.snapshot.savedSeq)return;
        const pending=this.pump();this.sending=pending;
        try {await pending;} finally {if(this.sending===pending)this.sending=null;}
    }
    private async saveCheckpoint():Promise<void>{
        if(this.initialized)await this.initialized;
        await this.localTail;
        if(this.sending||this.snapshot.structuralPending||this.snapshot.structuralUncertain||this.snapshot.competing||this.snapshot.conflicts.length)return;
        if(!this.operation&&(this.snapshot.materializedSeq??this.snapshot.editSeq)<=this.snapshot.savedSeq)return;
        const pending=this.pump();this.sending=pending;
        try{await pending;}finally{if(this.sending===pending)this.sending=null;}
    }
    private async pump(): Promise<void> {
        while(this.operation || (!this.snapshot.structuralPending&&!this.snapshot.structuralUncertain&&(this.snapshot.materializedSeq??this.snapshot.editSeq)>this.snapshot.savedSeq)){
            await this.localTail;
            if(!this.operation){
                this.operation={operation_id:crypto.randomUUID(),mutations:[{path:this.identity.path,writer_id:this.writerId,edit_seq:this.snapshot.materializedSeq??this.snapshot.editSeq,base_revision:this.snapshot.baseRevision,kind:'put',content_text:this.snapshot.body??'',language:this.descriptor?.language??'text'}],resolves:[...this.resolving]};
                await this.persist();
            }
            const operation=this.operation;
            this.update({status:'Saving to server',error:null});
            let result: FileMutationResult;
            try {result=await this.send(operation);} catch(error){this.update({status:'Server save failed',error:String((error as Error)?.message??error)});throw error;}
            const file=result.files.find(file=>file.path===this.identity.path);
            if(result.operation_id!==operation.operation_id){this.update({status:'Server save failed',error:'Invalid persistence acknowledgement.'});throw new Error('Invalid persistence acknowledgement.');}
            if(result.outcome!=='accepted'){
                this.operation=null;
                this.update({conflicts:result.conflicts,competing:file??this.snapshot.competing,status:'Conflict',error:result.outcome==='superseded'?'A newer writer sequence was already recorded.':null});
                await this.persist();
                throw new Error('File conflict needs explicit resolution.');
            }
            const submitted=operation.mutations[0];
            if(!file || file.writer_id!==this.writerId || file.edit_seq!==submitted.edit_seq || file.file_revision<=submitted.base_revision){this.update({status:'Server save failed',error:'Invalid persistence acknowledgement.'});throw new Error('Invalid persistence acknowledgement.');}
            let resolvedBody=submitted.content_text;
            let transformed=false;
            const acceptedGeneration=this.generation;
            if(resolvedBody!==undefined&&file.body_hash!==await bodyHash(resolvedBody)){
                // Declared semantic interactions can merge into different server bytes.
                // Retain the exact operation until that pinned result is available.
                try{resolvedBody=await this.read(file);transformed=true;}catch(error){this.update({status:'Server save failed',error:`Accepted file could not be read: ${String((error as Error)?.message??error)}`});throw error;}
            }
            this.operation=null;this.resolving=[];
            const competing=this.snapshot.competing && this.snapshot.competing.file_revision>file.file_revision?this.snapshot.competing:null;
            this.descriptor=file;
            this.update({...(resolvedBody!==undefined&&this.snapshot.editSeq===submitted.edit_seq&&this.generation===acceptedGeneration?{body:resolvedBody,...(transformed?{recovery:undefined,materializedSeq:submitted.edit_seq}:{})}:{}),savedSeq:submitted.edit_seq,baseRevision:file.file_revision,conflicts:[],competing,status:competing?'Conflict':this.snapshot.editSeq===submitted.edit_seq?'Saved':'Saved locally · waiting for server',error:null});
            await this.persist();
            if(competing)throw new Error('File conflict needs explicit resolution.');
            if(this.snapshot.editSeq===this.snapshot.savedSeq)await this.journal.remove(this.writerId,this.identity);
        }
    }
    async retry():Promise<void> {
        if(this.snapshot.status==='Local save failed')await this.persist();
        await this.flush();
    }
    async locallyDurable():Promise<void>{
        if(this.initialized)await this.initialized;
        while(true){const latest=this.localTail;await latest;if(latest===this.localTail)return;}
    }
    async beginStructural():Promise<DocumentSnapshot> {
        if(this.snapshot.structuralUncertain)throw new Error('Retry the pending file operation before renaming.');
        if(this.timer){clearTimeout(this.timer);this.timer=null;}
        this.update({structuralPending:true});
        try {
            if(this.sending)await this.sending;
            await this.localTail;
            await this.materializedBody();
            if(this.operation || this.snapshot.competing || this.snapshot.conflicts.length)throw new Error('Review versions or retry the pending save before renaming.');
            return this.snapshot;
        }catch(error){this.update({structuralPending:false});throw error;}
    }
    async finishStructural(accepted:boolean,uncertain=false):Promise<void> {
        if(accepted){
            this.operation=null;
            this.update({savedSeq:this.snapshot.editSeq,competing:null,conflicts:[],status:'Saved'});
            await this.localTail;await this.journal.remove(this.writerId,this.identity);
        }
        this.update({structuralPending:false,structuralUncertain:uncertain,...(uncertain?{status:'Server save failed' as const,error:'The file operation needs an exact retry. Your local edits are preserved.'}:{})});
        if(uncertain)await this.persist();
        if(!accepted)this.schedule();
    }
    async moveTo(file:FileDescriptor,capturedEditSeq:number):Promise<void>{
        await this.localTail;
        const previous={...this.identity},previousWriter=this.writerId;
        this.identity.path=file.path;
        this.descriptor=file;this.operation=null;
        const saved=Math.min(capturedEditSeq,this.snapshot.editSeq);
        this.update({baseRevision:file.file_revision,savedSeq:saved,competing:null,conflicts:[],structuralPending:false,structuralUncertain:false,status:this.snapshot.editSeq>saved?'Saving locally':'Saved'});
        // Commit the destination draft before removing the source recovery record.
        await this.persist();await this.journal.remove(previousWriter,previous);
        if(this.snapshot.editSeq>saved)this.schedule();
    }
    async resolve(body: string, expectedEditSeq=this.snapshot.editSeq, expectedRevision=this.snapshot.competing?.file_revision??this.snapshot.baseRevision): Promise<void> {
        await this.materializedBody();
        if(expectedEditSeq!==this.snapshot.editSeq || expectedRevision!==(this.snapshot.competing?.file_revision??this.snapshot.baseRevision))throw new Error('Versions changed. Review the latest versions before resolving.');
        if(this.sending)await this.sending.catch(()=>undefined);
        const revision=this.snapshot.competing?.file_revision??Math.max(this.snapshot.baseRevision,...this.snapshot.conflicts.map(c=>c.current_revision));
        this.resolving=this.snapshot.conflicts.map(c=>c.conflict_id);
        // Preserve editor bytes until this explicit choice is durably accepted.
        this.generation++;
        const seq=this.snapshot.editSeq+1;
        this.operation={operation_id:crypto.randomUUID(),mutations:[{path:this.identity.path,writer_id:this.writerId,edit_seq:seq,base_revision:revision,kind:'put',content_text:body,language:this.descriptor?.language??'text'}],resolves:[...this.resolving]};
        this.update({baseRevision:revision,competing:null,recovery:undefined,materializedSeq:seq,editSeq:seq,status:'Saving locally',error:null});
        await this.persist();await this.flush();
    }
    noteConflicts(conflicts:FileConflict[],current?:FileDescriptor):void {
        const own=conflicts.filter(conflict=>conflict.path===this.identity.path);
        if(!own.length)return;
        const merged=new Map(this.snapshot.conflicts.map(conflict=>[conflict.conflict_id,conflict]));
        for(const conflict of own)merged.set(conflict.conflict_id,conflict);
        this.update({conflicts:[...merged.values()],competing:current??this.descriptor,status:'Conflict'});
        void this.persist().catch(()=>undefined);
    }
    async candidateBody(conflict:FileConflict):Promise<string> {
        const base=this.descriptor;if(!base)throw new Error('File metadata is unavailable.');
        if(!conflict.candidate_hash)return '';
        return this.read({...base,file_revision:conflict.candidate_revision,body_hash:conflict.candidate_hash,deleted:false});
    }
    async competingBody(): Promise<string> {if(!this.snapshot.competing)throw new Error('No competing revision.');return this.snapshot.competing.deleted?'':this.read(this.snapshot.competing);}
    async recover(record: DocumentRecord): Promise<void> {
        // Explicit adoption leaves the original journal until durable acceptance.
        if(record.recovery)this.editSnapshot(record.recovery);else this.edit(record.body);
        await this.localTail;await this.materializedBody();
    }
}
