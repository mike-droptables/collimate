import {createHash} from 'node:crypto';
import { describe, expect, it, vi } from 'vitest';
import { FileDocument, documentKey, type DocumentIdentity, type DocumentJournal, type DocumentRecord } from './fileDocument';
import type { FileMutationResult } from './fileProtocol';

const identity = { origin: 'https://example.test', channel: 'channel', cellId: 'cell', path: 'notes.md' };
const metadata = { path: 'notes.md', file_revision: 7, body_hash: 'base', deleted: false, writer_id: 'other', edit_seq: 1, language: 'markdown', encoding: 'utf-8' as const };
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (error: Error) => void; const promise = new Promise<T>((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject}; }
const journalKey=(writerId:string,doc:DocumentIdentity=identity)=>`${documentKey(doc)}\u0000${writerId}`;
function journal() {
    const records = new Map<string, DocumentRecord>();
    const storage: DocumentJournal = {
        load: async doc => [...records.values()].filter(record=>documentKey(record.identity)===documentKey(doc)),
        save: vi.fn(async record => { records.set(journalKey(record.writerId,record.identity), structuredClone(record)); }),
        remove: vi.fn(async (writerId,doc) => { records.delete(journalKey(writerId,doc)); }),
    };
    return { storage, records };
}
function accepted(operationId: string, seq: number, revision: number, body: string): FileMutationResult {
    return {operation_id:operationId,outcome:'accepted',files:[{...metadata,file_revision:revision,writer_id:'writer',edit_seq:seq,body_hash:createHash('sha256').update(body).digest('hex')}],conflicts:[]};
}

describe('durable document ownership', () => {
    it('view handoff needs only the committed local snapshot while server and worker are unavailable',async()=>{
        const {storage}=journal(),worker=deferred<string>();const send=vi.fn().mockRejectedValue(new Error('offline'));
        const doc=new FileDocument({...identity},'writer',storage,send,async()=> 'base',0,()=>worker.promise);
        await doc.initialize(metadata);doc.editSnapshot({codecHash:'approved',snapshotId:'local-view-handoff',payload:{text:'latest'}});
        await doc.locallyDurable();expect(doc.getSnapshot()).toMatchObject({editSeq:1,localSeq:1,recovery:{snapshotId:'local-view-handoff'}});
        expect(send).not.toHaveBeenCalled();
    });
    it('structural capture stops after the existing inflight receipt without saving newer source edits',async()=>{
        const {storage}=journal(),receipt=deferred<FileMutationResult>();const send=vi.fn().mockImplementationOnce(()=>receipt.promise);
        const doc=new FileDocument({...identity},'writer',storage,send,async()=> 'base',100000);
        await doc.initialize(metadata);doc.edit('already sending');const saving=doc.flush();await vi.waitFor(()=>expect(send).toHaveBeenCalledTimes(1));
        doc.edit('atomic destination body');const barrier=doc.beginStructural();receipt.resolve(accepted(send.mock.calls[0][0].operation_id,1,8,'already sending'));
        await saving;expect(await barrier).toMatchObject({body:'atomic destination body',editSeq:2,savedSeq:1,structuralPending:true});
        expect(send).toHaveBeenCalledTimes(1);
        await doc.observe({...metadata,file_revision:9,body_hash:null,deleted:true});expect(doc.getSnapshot().body).toBe('atomic destination body');
    });
    it('uncertain structural ownership survives clean source deletion and keeps new edits local until replay',async()=>{
        const {storage,records}=journal(),send=vi.fn();const doc=new FileDocument({...identity},'writer',storage,send,async()=> 'clean source',0);
        await doc.initialize(metadata);await doc.beginStructural();await doc.finishStructural(false,true);
        await doc.observe({...metadata,file_revision:8,body_hash:null,deleted:true});expect(doc.getSnapshot().body).toBe('clean source');
        doc.edit('typing after refused rename');await doc.locallyDurable();await expect(doc.flush()).rejects.toThrow('pending file operation');
        expect(send).not.toHaveBeenCalled();expect(records.get(journalKey('writer'))).toMatchObject({structuralUncertain:true,body:'typing after refused rename'});
        const recovered=new FileDocument({...identity},'writer',storage,send,async()=> 'must not erase recovery',100000);
        await recovered.initialize({...metadata,file_revision:8,body_hash:null,deleted:true});expect(recovered.getSnapshot()).toMatchObject({structuralUncertain:true,body:'typing after refused rename'});
    });
    it('saves exact older worker checkpoints while continuous newer snapshots stay dirty',async()=>{
        const {storage,records}=journal(),first=deferred<string>(),second=deferred<string>(),third=deferred<string>();
        const derive=vi.fn().mockImplementationOnce(()=>first.promise).mockImplementationOnce(()=>second.promise).mockImplementationOnce(()=>third.promise);
        const send=vi.fn(async op=>accepted(op.operation_id,op.mutations[0].edit_seq,7+op.mutations[0].edit_seq,op.mutations[0].content_text));
        const doc=new FileDocument({...identity},'writer',storage,send,async()=> 'base',0,derive);
        await doc.initialize(metadata);doc.editSnapshot({codecHash:'approved',snapshotId:'one',payload:{text:'one'}});
        await vi.waitFor(()=>expect(derive).toHaveBeenCalledTimes(1));
        doc.editSnapshot({codecHash:'approved',snapshotId:'two',payload:{text:'two'}});first.resolve('derived one');
        await vi.waitFor(()=>expect(send).toHaveBeenCalledTimes(1));
        expect(send.mock.calls[0][0].mutations[0]).toMatchObject({edit_seq:1,content_text:'derived one',base_revision:7});
        expect(doc.getSnapshot()).toMatchObject({editSeq:2,materializedSeq:1,savedSeq:1,recovery:{snapshotId:'two'}});
        expect(records.get(journalKey('writer'))).toMatchObject({editSeq:2,savedSeq:1,recovery:{snapshotId:'two'}});
        doc.editSnapshot({codecHash:'approved',snapshotId:'three',payload:{text:'three'}});second.resolve('derived two');
        await vi.waitFor(()=>expect(send).toHaveBeenCalledTimes(2));
        expect(send.mock.calls[1][0].mutations[0]).toMatchObject({edit_seq:2,content_text:'derived two',base_revision:8});
        expect(doc.getSnapshot()).toMatchObject({editSeq:3,savedSeq:2,recovery:{snapshotId:'three'}});
        third.resolve('derived three');await doc.flush();expect(doc.getSnapshot()).toMatchObject({editSeq:3,savedSeq:3,status:'Saved'});
    });
    it('renderer sidecar reads wait for initial recovery instead of returning an empty body',async()=>{
        const {storage}=journal(),read=deferred<string>();
        const doc=new FileDocument({...identity},'writer',storage,vi.fn(),()=>read.promise,100000);
        const initializing=doc.initialize(metadata),body=doc.materializedBody();let resolved=false;void body.then(()=>{resolved=true;});
        await Promise.resolve();expect(resolved).toBe(false);read.resolve('pinned sidecar');
        await initializing;expect(await body).toBe('pinned sidecar');
    });
    it('adopts a pinned server semantic merge after accepting the submitted sequence',async()=>{
        const {storage}=journal(),merged='local comment\nconcurrent server comment';
        const send=vi.fn(async op=>accepted(op.operation_id,op.mutations[0].edit_seq,8,merged));
        const read=vi.fn(async descriptor=>descriptor.file_revision===7?'base':merged);
        const doc=new FileDocument({...identity},'writer',storage,send,read,100000);
        await doc.initialize(metadata);doc.edit('local comment');await doc.flush();
        expect(read).toHaveBeenLastCalledWith(expect.objectContaining({file_revision:8,body_hash:createHash('sha256').update(merged).digest('hex')}));
        expect(doc.getSnapshot()).toMatchObject({body:merged,editSeq:1,savedSeq:1,baseRevision:8,status:'Saved'});
    });
    it('a delayed merged accepted-body read cannot overwrite newer raw snapshots',async()=>{
        const {storage}=journal(),mergedRead=deferred<string>();
        const send=vi.fn().mockImplementationOnce(async op=>accepted(op.operation_id,1,8,'merged one')).mockImplementation(async op=>accepted(op.operation_id,op.mutations[0].edit_seq,9,op.mutations[0].content_text));
        const read=vi.fn().mockResolvedValueOnce('base').mockImplementationOnce(()=>mergedRead.promise);
        const doc=new FileDocument({...identity},'writer',storage,send,read,100000,async recovery=>String((recovery.payload as {text:string}).text));
        await doc.initialize(metadata);doc.edit('one');const saving=doc.flush();
        await vi.waitFor(()=>expect(read).toHaveBeenCalledTimes(2));
        doc.editSnapshot({codecHash:'approved',snapshotId:'newer-raw',payload:{text:'newer typing'}});
        mergedRead.resolve('merged one');await saving;await doc.flush();
        expect(send.mock.calls[1][0].mutations[0]).toMatchObject({base_revision:8,edit_seq:2,content_text:'newer typing'});
        expect(doc.getSnapshot()).toMatchObject({body:'newer typing',savedSeq:2,recovery:{snapshotId:'newer-raw'}});
    });
    it('explicit recovery adopts exact raw payload rather than an older materialized body',async()=>{
        const {storage}=journal();const doc=new FileDocument({...identity},'writer',storage,vi.fn(),async()=> 'accepted',100000,async recovery=>String((recovery.payload as {text:string}).text));
        await doc.initialize(metadata);
        await doc.recover({identity:{...identity},writerId:'another-window',body:'obsolete Markdown',editSeq:9,materializedSeq:8,localSeq:9,savedSeq:0,baseRevision:7,operation:null,conflicts:[],recovery:{codecHash:'approved',snapshotId:'raw-adoption',payload:{text:'exact transaction'}}});
        expect(doc.getSnapshot()).toMatchObject({body:'exact transaction',editSeq:1,materializedSeq:1,recovery:{snapshotId:'raw-adoption'}});
    });
    it('journals exact structured snapshots immediately and materializes only the latest sequence before sending',async()=>{
        const {storage,records}=journal(),first=deferred<string>();
        const derive=vi.fn().mockImplementationOnce(()=>first.promise).mockImplementation(async recovery=>`derived ${recovery.payload.text}`);
        const send=vi.fn(async op=>accepted(op.operation_id,op.mutations[0].edit_seq,8,op.mutations[0].content_text));
        const doc=new FileDocument({...identity},'writer',storage,send,async()=> 'base',100000,derive);
        await doc.initialize(metadata);
        doc.editSnapshot({codecHash:'approved',snapshotId:'one',payload:{text:'one'}});
        await vi.waitFor(()=>expect(derive).toHaveBeenCalledTimes(1));
        doc.editSnapshot({codecHash:'approved',snapshotId:'two',payload:{text:'two'}});
        await vi.waitFor(()=>expect(records.get(journalKey('writer'))?.recovery?.snapshotId).toBe('two'));
        expect(doc.getDocumentSnapshot()).toMatchObject({content:'base',editSeq:2,materializedSeq:0,recovery:{snapshotId:'two'}});
        expect(send).not.toHaveBeenCalled();first.resolve('derived one');await doc.flush();
        expect(send.mock.calls[0][0].mutations[0]).toMatchObject({edit_seq:2,content_text:'derived two'});
        expect(doc.getSnapshot()).toMatchObject({body:'derived two',savedSeq:2,materializedSeq:2,status:'Saved'});
    });
    it('reload recovers a locally committed raw snapshot before any derived Markdown exists',async()=>{
        const {storage}=journal(),never=deferred<string>();
        const old=new FileDocument({...identity},'writer',storage,vi.fn(),async()=> 'base',100000,()=>never.promise);
        await old.initialize(metadata);old.editSnapshot({codecHash:'approved',snapshotId:'raw',payload:{exact:'last transaction'}});
        await vi.waitFor(()=>expect(old.getSnapshot().localSeq).toBe(1));
        const send=vi.fn(async op=>accepted(op.operation_id,1,8,op.mutations[0].content_text));
        const recovered=new FileDocument({...identity},'writer',storage,send,async()=> 'wrong server base',100000,async recovery=>String((recovery.payload as {exact:string}).exact));
        await recovered.initialize(metadata);expect(recovered.getDocumentSnapshot().recovery?.snapshotId).toBe('raw');
        await recovered.flush();expect(send.mock.calls[0][0].mutations[0].content_text).toBe('last transaction');
    });
    it('an SSE echo of the exact uncertain own operation is not a competing writer',async()=>{
        const {storage}=journal();const send=vi.fn().mockRejectedValueOnce(new Error('lost response')).mockImplementation(async op=>accepted(op.operation_id,op.mutations[0].edit_seq,op.mutations[0].edit_seq===1?8:9,op.mutations[0].content_text));
        const doc=new FileDocument({...identity},'writer',storage,send,async()=> 'base',100000);
        await doc.initialize(metadata);doc.edit('one');await expect(doc.flush()).rejects.toThrow('lost response');
        const exact=send.mock.calls[0][0];await doc.observe({...metadata,file_revision:8,writer_id:'writer',edit_seq:1});
        expect(doc.getSnapshot().competing).toBeNull();expect(doc.getSnapshot().savedSeq).toBe(0);
        doc.edit('two');await doc.flush();expect(send.mock.calls[1][0]).toEqual(exact);
        expect(send.mock.calls[2][0].mutations[0]).toMatchObject({content_text:'two',edit_seq:2,base_revision:8});
    });
    it('retargets edits made after an uncertain rename without falsely acknowledging them',async()=>{
        const {storage,records}=journal();const doc=new FileDocument({...identity},'window:notes.md',storage,vi.fn(),async()=> 'base',100000);
        await doc.initialize(metadata);doc.edit('captured rename');await doc.beginStructural();await doc.finishStructural(false);
        doc.edit('typed after lost response');
        await doc.moveTo({...metadata,path:'renamed.md',file_revision:1},1);
        expect(doc.identity.path).toBe('renamed.md');expect(doc.getSnapshot()).toMatchObject({body:'typed after lost response',editSeq:2,savedSeq:1,baseRevision:1});
        expect(doc.writerId).toBe('window:notes.md');
        expect(records.get(journalKey('window:notes.md',{...identity,path:'renamed.md'}))).toMatchObject({identity:expect.objectContaining({path:'renamed.md'}),body:'typed after lost response',savedSeq:1});
    });
    it('journals typing in conflict without repeatedly autosaving candidates',async()=>{
        const {storage}=journal();const send=vi.fn();
        const doc=new FileDocument(identity,'writer',storage,send,async()=> 'base',0);
        await doc.initialize(metadata);doc.edit('local');
        await doc.observe({...metadata,file_revision:8,writer_id:'competitor'});
        doc.edit('more local');await new Promise(resolve=>setTimeout(resolve,20));
        expect(send).not.toHaveBeenCalled();expect(doc.getSnapshot()).toMatchObject({body:'more local',localSeq:2,status:'Conflict'});
        await expect(doc.flush()).rejects.toThrow(/conflict/i);
    });
    it('rejects a resolution whose reviewed local sequence changed',async()=>{
        const {storage}=journal();const doc=new FileDocument(identity,'writer',storage,vi.fn(),async()=> 'base',10000);
        await doc.initialize(metadata);doc.edit('one');await doc.observe({...metadata,file_revision:8});doc.edit('two');
        await expect(doc.resolve('reviewed one',1,8)).rejects.toThrow(/Versions changed/);
        expect(doc.getSnapshot().body).toBe('two');
    });
    it('journals every serialized edit before dispatch and shows the local durability receipt', async () => {
        const local = deferred<void>(); const {storage}=journal(); storage.save=vi.fn(()=>local.promise);
        const send=vi.fn(async op=>accepted(op.operation_id,1,8,op.mutations[0].content_text));
        const doc=new FileDocument(identity,'writer',storage,send,async()=> 'base body',0);
        await doc.initialize(metadata);
        doc.edit('last keystroke');
        expect(doc.getSnapshot().status).toBe('Saving locally');
        expect(send).not.toHaveBeenCalled();
        local.resolve(); await doc.flush();
        expect(storage.save).toHaveBeenCalledWith(expect.objectContaining({body:'last keystroke',editSeq:1}));
        expect(send).toHaveBeenCalledTimes(1);
        expect(doc.getSnapshot().status).toBe('Saved');
    });
    it('an older acknowledgement cannot clean a newer edit and queues it against the acknowledged revision', async () => {
        const first=deferred<FileMutationResult>(); const {storage}=journal();
        const send=vi.fn().mockImplementationOnce(()=>first.promise).mockImplementation(async op=>accepted(op.operation_id,op.mutations[0].edit_seq,9,op.mutations[0].content_text));
        const doc=new FileDocument(identity,'writer',storage,send,async()=> 'base body',0);
        await doc.initialize(metadata); doc.edit('one'); const saving=doc.flush();
        await vi.waitFor(()=>expect(send).toHaveBeenCalledTimes(1));
        doc.edit('two'); first.resolve(accepted(send.mock.calls[0][0].operation_id,1,8,'one'));
        await saving;
        expect(send.mock.calls[1][0].mutations[0]).toMatchObject({content_text:'two',base_revision:8,edit_seq:2});
        expect(doc.getSnapshot()).toMatchObject({body:'two',savedSeq:2,status:'Saved'});
    });
    it('retains exact uncertain operation across failure and reload, reusing its receipt identity', async () => {
        const {storage,records}=journal(); const send=vi.fn().mockRejectedValue(new Error('offline'));
        const doc=new FileDocument(identity,'writer',storage,send,async()=> 'base body',0);
        await doc.initialize(metadata); doc.edit('recover me'); await expect(doc.flush()).rejects.toThrow('offline');
        const operation=records.get(journalKey('writer'))!.operation!;
        const retry=vi.fn(async op=>accepted(op.operation_id,1,8,op.mutations[0].content_text));
        const reopened=new FileDocument(identity,'writer',storage,retry,async()=> 'server old',0);
        await reopened.initialize(metadata);
        expect(reopened.getSnapshot().body).toBe('recover me'); await reopened.flush();
        expect(retry).toHaveBeenCalledWith(operation);
    });
    it('dirty text survives delayed pinned reads and external deletion', async () => {
        const body=deferred<string>(); const {storage}=journal();
        const doc=new FileDocument(identity,'writer',storage,vi.fn(),()=>body.promise,10000);
        const initialization=doc.initialize(metadata); doc.edit('typed before read'); body.resolve('old body'); await initialization;
        await doc.observe({...metadata,file_revision:8,deleted:true,body_hash:null});
        expect(doc.getSnapshot()).toMatchObject({body:'typed before read',competing:expect.objectContaining({deleted:true}),status:'Conflict'});
    });
    it('preserves competing candidates and requires an explicit guarded resolution', async () => {
        const {storage}=journal(); const conflict={conflict_id:'conflict',path:'notes.md',base_revision:7,current_revision:8,candidate_revision:9,candidate_hash:'local',writer_id:'writer',edit_seq:1};
        const send=vi.fn().mockImplementationOnce(async op=>({operation_id:op.operation_id,outcome:'conflict',files:[{...metadata,file_revision:8}],conflicts:[conflict]})).mockImplementation(async op=>accepted(op.operation_id,2,10,op.mutations[0].content_text));
        const doc=new FileDocument(identity,'writer',storage,send,async()=> 'competing body',0);
        await doc.initialize(metadata); doc.edit('local body'); await expect(doc.flush()).rejects.toThrow(/conflict/i);
        expect(doc.getSnapshot().body).toBe('local body'); expect(doc.getSnapshot().conflicts).toEqual([conflict]);
        await doc.resolve('merged body');
        expect(send.mock.calls[1][0]).toMatchObject({resolves:['conflict'],mutations:[{base_revision:8,content_text:'merged body'}]});
    });
    it('does not claim local safety or call the server when IndexedDB refuses the edit', async () => {
        const {storage}=journal(); storage.save=vi.fn().mockRejectedValue(new Error('quota exceeded')); const send=vi.fn();
        const doc=new FileDocument(identity,'writer',storage,send,async()=> 'base body',0);
        await doc.initialize(metadata); doc.edit('only in memory'); await expect(doc.flush()).rejects.toThrow('quota exceeded');
        expect(doc.getSnapshot()).toMatchObject({body:'only in memory',status:'Local save failed',localSeq:0}); expect(send).not.toHaveBeenCalled();
    });
    it('keeps other window journals distinct and exposes abandoned recovery instead of stealing it', async () => {
        const {storage,records}=journal();
        records.set(journalKey('other-window'),{identity,writerId:'other-window',body:'other text',editSeq:3,baseRevision:7,savedSeq:0,localSeq:3,operation:null,conflicts:[]});
        const doc=new FileDocument(identity,'writer',storage,vi.fn(),async()=> 'server text',10000);
        await doc.initialize(metadata);
        expect(doc.getSnapshot().body).toBe('server text'); expect(doc.getSnapshot().recoveries).toHaveLength(1);
        expect(records.get(journalKey('other-window'))!.body).toBe('other text');
    });
});
