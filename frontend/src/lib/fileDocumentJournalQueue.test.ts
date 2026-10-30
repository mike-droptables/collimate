import {createHash} from 'node:crypto';
import {describe,expect,it,vi} from 'vitest';
import {FileDocument,type DocumentJournal,type DocumentRecord} from './fileDocument';
import type {FileMutationResult,FileOperation} from './fileProtocol';

const identity={origin:'https://example.test',channel:'channel',cellId:'cell',path:'notes.md'};
const descriptor={path:'notes.md',file_revision:7,body_hash:'base',deleted:false,writer_id:'other',edit_seq:1,language:'markdown',encoding:'utf-8' as const};
function deferred<T>(){let resolve!:(value:T)=>void;let reject!:(error:Error)=>void;const promise=new Promise<T>((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function raw(sequence:number){return {codecHash:'approved',snapshotId:`view:${sequence}`,payload:{text:`latest ${sequence}`}};}
function controlledJournal(){
    const writes:{record:DocumentRecord;commit:ReturnType<typeof deferred<void>>}[]=[];
    let stored:DocumentRecord|undefined;
    const journal:DocumentJournal={
        load:async()=>stored?[structuredClone(stored)]:[],
        save:async record=>{const write={record:structuredClone(record),commit:deferred<void>()};writes.push(write);await write.commit.promise;stored=write.record;},
        remove:async()=>{stored=undefined;},
    };
    return {journal,writes,stored:()=>stored};
}
const accepted=(operation:FileOperation):FileMutationResult=>({operation_id:operation.operation_id,outcome:'accepted',files:[{...descriptor,file_revision:8,writer_id:'writer',edit_seq:operation.mutations[0].edit_seq,body_hash:createHash('sha256').update(operation.mutations[0].content_text!).digest('hex')}],conflicts:[]});

describe('document journal backpressure',()=>{
    it('coalesces text drafts through the same queue and does not drop their latest bytes',async()=>{
        const {journal,writes,stored}=controlledJournal();
        const doc=new FileDocument({...identity},'writer',journal,vi.fn(),async()=> 'base',100000);
        await doc.initialize(descriptor);doc.edit('first');await vi.waitFor(()=>expect(writes).toHaveLength(1));
        for(let sequence=2;sequence<=20;sequence++)doc.edit(`text ${sequence}`);
        const handoff=doc.locallyDurable();writes[0].commit.resolve();await vi.waitFor(()=>expect(writes).toHaveLength(2));
        expect(writes[1].record).toMatchObject({body:'text 20',editSeq:20,materializedSeq:20});
        writes[1].commit.resolve();await handoff;expect(stored()?.body).toBe('text 20');
        expect(doc.getSnapshot()).toMatchObject({editSeq:20,localSeq:20});
    });
    it('commits one latest pending snapshot and releases older waiters only after that commit',async()=>{
        const {journal,writes,stored}=controlledJournal(),worker=deferred<string>();
        const doc=new FileDocument({...identity},'writer',journal,vi.fn(),async()=> 'base',100000,()=>worker.promise);
        await doc.initialize(descriptor);doc.editSnapshot(raw(1));
        await vi.waitFor(()=>expect(writes).toHaveLength(1));
        doc.editSnapshot(raw(2));let durable=false;const handoff=doc.locallyDurable().then(()=>{durable=true;});
        for(let sequence=3;sequence<=20;sequence++)doc.editSnapshot(raw(sequence));
        expect(writes).toHaveLength(1);expect(durable).toBe(false);
        writes[0].commit.resolve();await vi.waitFor(()=>expect(writes).toHaveLength(2));
        expect(writes[1].record).toMatchObject({editSeq:20,recovery:raw(20)});
        expect(doc.getSnapshot().localSeq).toBe(1);expect(durable).toBe(false);
        writes[1].commit.resolve();await handoff;
        expect(doc.getSnapshot()).toMatchObject({editSeq:20,localSeq:20});expect(stored()?.recovery).toEqual(raw(20));
        const recovered=new FileDocument({...identity},'writer',journal,vi.fn(),async()=> 'wrong base',100000,()=>worker.promise);
        await recovered.initialize(descriptor);expect(recovered.getDocumentSnapshot().recovery).toEqual(raw(20));
    });
    it('rejects shared durability on an aborted latest write and retries the exact current snapshot',async()=>{
        const {journal,writes}=controlledJournal(),worker=deferred<string>();
        const doc=new FileDocument({...identity},'writer',journal,vi.fn(),async()=> 'base',100000,()=>worker.promise);
        await doc.initialize(descriptor);doc.editSnapshot(raw(1));await vi.waitFor(()=>expect(writes).toHaveLength(1));
        doc.editSnapshot(raw(2));const first=expect(doc.locallyDurable()).rejects.toThrow('disk full');
        doc.editSnapshot(raw(3));const second=expect(doc.locallyDurable()).rejects.toThrow('disk full');
        writes[0].commit.resolve();await vi.waitFor(()=>expect(writes).toHaveLength(2));
        writes[1].commit.reject(new Error('disk full'));await Promise.all([first,second]);
        expect(doc.getSnapshot()).toMatchObject({localSeq:1,editSeq:3,status:'Local save failed',recovery:raw(3)});
        void doc.retry();await vi.waitFor(()=>expect(writes).toHaveLength(3));
        expect(writes[2].record).toMatchObject({editSeq:3,recovery:raw(3)});writes[2].commit.resolve();
        await doc.locallyDurable();expect(doc.getSnapshot().localSeq).toBe(3);
    });
    it('preserves the exact operation barrier before dispatch while newer raw writes coalesce',async()=>{
        const {journal,writes}=controlledJournal(),worker=deferred<string>(),receipt=deferred<FileMutationResult>();
        const send=vi.fn(()=>receipt.promise);
        const doc=new FileDocument({...identity},'writer',journal,send,async()=> 'base',100000,()=>worker.promise);
        await doc.initialize(descriptor);doc.edit('exact submitted body');const saving=doc.flush();
        await vi.waitFor(()=>expect(writes).toHaveLength(1));writes[0].commit.resolve();
        await vi.waitFor(()=>expect(writes).toHaveLength(2));
        const operation=writes[1].record.operation!;expect(operation.mutations[0]).toMatchObject({edit_seq:1,content_text:'exact submitted body'});
        for(let sequence=2;sequence<=20;sequence++)doc.editSnapshot(raw(sequence));
        expect(send).not.toHaveBeenCalled();expect(writes[1].record).toMatchObject({editSeq:1,operation});
        writes[1].commit.resolve();await vi.waitFor(()=>expect(send).toHaveBeenCalledWith(operation));
        await vi.waitFor(()=>expect(writes).toHaveLength(3));
        expect(writes[2].record).toMatchObject({editSeq:20,recovery:raw(20),operation});
        writes[2].commit.resolve();receipt.resolve(accepted(operation));
        await vi.waitFor(()=>expect(writes).toHaveLength(4));
        expect(writes[3].record).toMatchObject({operation:null,savedSeq:1,editSeq:20,recovery:raw(20)});
        writes[3].commit.resolve();await saving;
        expect(doc.getSnapshot()).toMatchObject({savedSeq:1,editSeq:20,localSeq:20});
    });
});
