import {appendFileSync} from 'node:fs';
import {IDBFactory} from 'fake-indexeddb';
import {beforeEach,describe,expect,it,vi} from 'vitest';
import {FileDocument,type DocumentRecord} from './fileDocument';
import type {FileDescriptor} from './fileProtocol';
let storage:typeof import('./fileDocumentStorage');
const identity={origin:'https://example.test',channel:'channel-uuid',cellId:'cell',path:'notes.txt'};
const metadata:FileDescriptor={path:'notes.txt',file_revision:1,body_hash:'server',deleted:false,language:'text',encoding:'utf-8',writer_id:'server',edit_seq:1};
const record=(writerId:string,body:string):DocumentRecord=>({identity,writerId,body,editSeq:1,localSeq:1,savedSeq:0,baseRevision:1,operation:null,conflicts:[]});
beforeEach(async()=>{vi.resetModules();vi.stubGlobal('indexedDB',new IDBFactory());storage=await import('./fileDocumentStorage');});
describe('IndexedDB durability receipts',()=>{
    it('resolves after the committed transaction is visible to a fresh readonly transaction',async()=>{
        await storage.documentJournal.save(record('window-one','last keystroke'));
        expect(await storage.documentJournal.load(identity)).toEqual([record('window-one','last keystroke')]);
    });
    it('scopes recoverable bodies by server, immutable channel, cell, path and writer',async()=>{
        await storage.documentJournal.save(record('window-one','one'));await storage.documentJournal.save(record('window-two','two'));
        expect(await storage.documentJournal.load(identity)).toHaveLength(2);
        expect(await storage.documentJournal.load({...identity,origin:'https://another.test'})).toEqual([]);
        expect(await storage.documentJournal.load({...identity,channel:'another-uuid'})).toEqual([]);
        await storage.documentJournal.remove('window-one',identity);
        expect(await storage.documentJournal.load(identity)).toEqual([record('window-two','two')]);
    });
    it('a new owner recovers the last locally committed snapshot and uncertain operation',async()=>{
        const failing=new FileDocument(identity,'writer',storage.documentJournal,async()=>{throw new Error('offline');},async()=> 'server old',100000);
        await failing.initialize(metadata);failing.edit('durable local');await expect(failing.flush()).rejects.toThrow('offline');
        const recovered=new FileDocument(identity,'writer',storage.documentJournal,async()=>{throw new Error('offline');},async()=> 'must not replace local',100000);
        await recovered.initialize(metadata);
        expect(recovered.getSnapshot()).toMatchObject({body:'durable local',localSeq:1,status:'Saved locally · waiting for server'});
        expect((await storage.documentJournal.load(identity))[0].operation?.mutations[0].content_text).toBe('durable local');
    });
    it('retains an uncertain structural operation independently of an editor mount',async()=>{
        const operation={operation_id:'operation',mutations:[{path:'new.txt',writer_id:'writer',edit_seq:1,base_revision:0,kind:'put' as const,content_text:''}],resolves:[]};
        const pending={key:operation.operation_id,origin:identity.origin,channel:identity.channel,cellId:identity.cellId,operation};
        await storage.operationJournal.save(pending);expect(await storage.operationJournal.load()).toEqual([pending]);
        await storage.operationJournal.remove(pending.key);expect(await storage.operationJournal.load()).toEqual([]);
    });
    it('upgrades an existing draft journal and commits raw recovery plus its offline decoder',async()=>{
        const raw={...record('writer','older materialization'),editSeq:2,recovery:{codecHash:'approved-hash',snapshotId:'snapshot-two',payload:{type:'doc',content:[{type:'text',text:'latest edit'}]}},materializedSeq:1};
        await new Promise<void>((resolve,reject)=>{
            const opening=indexedDB.open('collimate-documents',2);
            opening.onupgradeneeded=()=>{const db=opening.result;db.createObjectStore('documents',{keyPath:'key'}).createIndex('document','document');db.createObjectStore('operations',{keyPath:'key'});};
            opening.onerror=()=>reject(opening.error);
            opening.onsuccess=()=>{const db=opening.result,tx=db.transaction('documents','readwrite');tx.objectStore('documents').put({key:'existing-record',document:JSON.stringify([identity.origin,identity.channel,identity.cellId,identity.path]),record:raw});tx.oncomplete=()=>{db.close();resolve();};tx.onabort=()=>reject(tx.error);};
        });
        const codec={id:'markdown',version:1,implementationHash:'approved-hash',source:'trusted decoder'};
        await storage.codecJournal.save(codec);
        expect(await storage.codecJournal.load(codec.implementationHash)).toEqual(codec);
        expect(await storage.documentJournal.load(identity)).toEqual([raw]);
        const owner=new FileDocument(identity,'writer',storage.documentJournal,async()=>{throw new Error('offline');},async()=> 'must not replace raw draft',100000,async recovery=>String((recovery.payload as {content:{text:string}[]}).content[0].text));
        await owner.initialize(metadata);expect(owner.getDocumentSnapshot().recovery).toEqual(raw.recovery);
        expect(await owner.materializedBody()).toBe('latest edit');
        expect((await storage.documentJournal.load(identity))[0]).toMatchObject({body:'latest edit',editSeq:2,materializedSeq:2,recovery:raw.recovery});
    });
    it.each([1024,50000,200000,1000000])('measures per-keystroke ownership and committed local transactions for %i bytes',async size=>{
        const initial='a'.repeat(size);const samples:number[]=[];
        const document=new FileDocument(identity,'writer',storage.documentJournal,async operation=>({operation_id:operation.operation_id,outcome:'accepted',files:[{...metadata,file_revision:2,writer_id:'writer',edit_seq:operation.mutations[0].edit_seq}],conflicts:[]}),async()=>initial,100000);
        await document.initialize(metadata);const started=performance.now();
        for(let index=0;index<30;index++){const before=performance.now();document.edit(`${initial}${index}`);samples.push(performance.now()-before);}
        await vi.waitFor(()=>expect(document.getSnapshot().localSeq).toBe(30));
        const elapsed=performance.now()-started;const sorted=[...samples].sort((a,b)=>a-b);
        const measurement={measurement:'document-edit-indexeddb-test-adapter',bytes:size,edits:30,synchronousP95Ms:sorted[Math.floor(sorted.length*0.95)],localCommitTotalMs:elapsed};
        if(process.env.PERSISTENCE_MEASUREMENTS)appendFileSync(process.env.PERSISTENCE_MEASUREMENTS,JSON.stringify(measurement)+'\n');
        expect(document.getSnapshot().body).toBe(`${initial}29`);
        expect((await storage.documentJournal.load(identity))[0].body).toBe(`${initial}29`);
    });
});
