import {appendFileSync} from 'node:fs';
import {IDBFactory} from 'fake-indexeddb';
import {beforeEach,describe,expect,it,vi} from 'vitest';
import {FileDocument,type DocumentRecord} from './fileDocument';
import type {FileDescriptor} from './fileProtocol';
import type {JournaledCodec} from './fileSnapshotCodec';
vi.mock('./api/client',()=>({getApiUrl:()=>'/api'}));
let storage:typeof import('./fileDocumentStorage');
const identity={origin:'https://example.test',channel:'channel-uuid',cellId:'cell',path:'notes.txt'};
const metadata:FileDescriptor={path:'notes.txt',file_revision:1,body_hash:'server',deleted:false,language:'text',encoding:'utf-8',writer_id:'server',edit_seq:1};
const record=(writerId:string,body:string):DocumentRecord=>({identity,writerId,body,editSeq:1,localSeq:1,savedSeq:0,baseRevision:1,operation:null,conflicts:[]});
/** Leave the database as an older build left it: created at ``version`` with
 *  ``stores``, holding what ``fill``'s transaction writes. */
const older=(version:number,stores:(db:IDBDatabase)=>void,fill:(db:IDBDatabase)=>IDBTransaction)=>new Promise<void>((resolve,reject)=>{
    const opening=indexedDB.open('collimate-documents',version);
    opening.onupgradeneeded=()=>stores(opening.result);opening.onerror=()=>reject(opening.error);
    opening.onsuccess=()=>{const db=opening.result,tx=fill(db);tx.oncomplete=()=>{db.close();resolve();};tx.onabort=()=>reject(tx.error);};
});
/** The database as the storage module left it, read on a second connection. */
const opened=()=>new Promise<{version:number;stores:string[];codecs:unknown[]}>((resolve,reject)=>{
    const opening=indexedDB.open('collimate-documents');opening.onerror=()=>reject(opening.error);
    opening.onsuccess=()=>{const db=opening.result,tx=db.transaction('codecs','readonly'),read=tx.objectStore('codecs').getAll();tx.oncomplete=()=>{resolve({version:db.version,stores:Array.from(db.objectStoreNames).sort(),codecs:read.result});db.close();};tx.onabort=()=>reject(tx.error);};
});
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
    it('pins the schema version with the codec record shape',()=>{
        // Changing a store's record shape bumps both: SCHEMA_VERSION so the
        // upgrade hook runs over older records, CODEC_RECORD_SHAPE so a record
        // of the old shape reads as absent. Update this pair only together.
        expect([storage.SCHEMA_VERSION,storage.CODEC_RECORD_SHAPE]).toEqual([4,1]);
    });
    it('wipes a version-2 draft journal on upgrade and journals fresh raw recovery plus its offline decoder',async()=>{
        const raw={...record('writer','older materialization'),editSeq:2,recovery:{codecHash:'approved-hash',snapshotId:'snapshot-two',payload:{type:'doc',content:[{type:'text',text:'latest edit'}]}},materializedSeq:1};
        await older(2,db=>{db.createObjectStore('documents',{keyPath:'key'}).createIndex('document','document');db.createObjectStore('operations',{keyPath:'key'});},db=>{const tx=db.transaction('documents','readwrite');tx.objectStore('documents').put({key:'existing-record',document:JSON.stringify([identity.origin,identity.channel,identity.cellId,identity.path]),record:raw});return tx;});
        expect(await storage.documentJournal.load(identity)).toEqual([]);expect(await storage.operationJournal.load()).toEqual([]);
        expect(await opened()).toEqual({version:4,stores:['codecs','documents','operations'],codecs:[]});
        const codec:JournaledCodec={id:'markdown',version:1,implementationHash:'approved-hash',source:'trusted decoder',shape:storage.CODEC_RECORD_SHAPE,channels:['ch']};
        await storage.codecJournal.save(codec);expect(await storage.codecJournal.load(codec.implementationHash)).toEqual(codec);
        await storage.documentJournal.save(raw);
        const owner=new FileDocument(identity,'writer',storage.documentJournal,async()=>{throw new Error('offline');},async()=> 'must not replace raw draft',100000,async recovery=>String((recovery.payload as {content:{text:string}[]}).content[0].text));
        await owner.initialize(metadata);expect(owner.getDocumentSnapshot().recovery).toEqual(raw.recovery);
        expect(await owner.materializedBody()).toBe('latest edit');
        expect((await storage.documentJournal.load(identity))[0]).toMatchObject({body:'latest edit',editSeq:2,materializedSeq:2,recovery:raw.recovery});
    });
    it('wipes a version-3 codec journal of channel-less records, so the codec registers afresh',async()=>{
        const source='self.onmessage = () => {};',hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(source)))).map(n=>n.toString(16).padStart(2,'0')).join('');
        const codec={id:'synthetic-codec',version:1,implementationHash:hash,source};
        await older(3,db=>{db.createObjectStore('codecs',{keyPath:'implementationHash'});db.createObjectStore('documents',{keyPath:'key'}).createIndex('document','document');db.createObjectStore('operations',{keyPath:'key'});},db=>{const tx=db.transaction('codecs','readwrite');tx.objectStore('codecs').put({...codec,approved:true});return tx;});
        expect(await storage.codecJournal.load(hash)).toBeUndefined();
        expect(await opened()).toEqual({version:4,stores:['codecs','documents','operations'],codecs:[]});
        vi.stubGlobal('fetch',vi.fn(async()=>new Response(JSON.stringify({codecs:[{id:codec.id,version:codec.version,implementationHash:hash}]}))));
        const registry=await import('./fileSnapshotCodec');await registry.registerSnapshotCodec('channel',codec);
        expect(await storage.codecJournal.load(hash)).toEqual({...codec,shape:1,channels:['channel']});
    });
    it('reads a codec record of another shape as absent',async()=>{
        const codec={id:'synthetic-codec',version:1,implementationHash:'other-shape',source:'decoder',channels:['ch']};
        await storage.codecJournal.save({...codec,shape:2} as unknown as JournaledCodec);
        expect(await storage.codecJournal.load(codec.implementationHash)).toBeUndefined();
        expect((await opened()).codecs).toEqual([{...codec,shape:2}]);
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
