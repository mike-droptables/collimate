import { documentKey, type DocumentIdentity, type DocumentJournal, type DocumentRecord } from './fileDocument';

/** Bump when any store's record shape changes (``CODEC_RECORD_SHAPE`` among
 *  them). The upgrade hook below is the one place a later version migrates
 *  (or, today, wipes). */
export const SCHEMA_VERSION=4;
/** The shape of a journaled codec record; a record of another shape reads as
 *  absent. Changing it goes with a ``SCHEMA_VERSION`` bump. */
export const CODEC_RECORD_SHAPE=1;
function upgrade(db:IDBDatabase,_from:number):void{
    // Pre-release: no data from earlier schemas is kept. When migration
    // becomes a requirement, replace the wipe for the relevant `from`
    // with a per-store migration; keep the structure.
    for(const name of Array.from(db.objectStoreNames))db.deleteObjectStore(name);
    db.createObjectStore('codecs',{keyPath:'implementationHash'});
    db.createObjectStore('documents',{keyPath:'key'}).createIndex('document','document');
    db.createObjectStore('operations',{keyPath:'key'});
}
let opening: Promise<IDBDatabase> | null=null;
function database(): Promise<IDBDatabase> {
    if(opening)return opening;
    opening=new Promise((resolve,reject)=>{
        if(typeof indexedDB==='undefined'){reject(new Error('Browser draft storage is unavailable.'));return;}
        const request=indexedDB.open('collimate-documents',SCHEMA_VERSION);
        request.onupgradeneeded=event=>upgrade(request.result,event.oldVersion);
        request.onerror=()=>reject(request.error??new Error('Could not open browser draft storage.'));
        request.onblocked=()=>reject(new Error('Close older Collimate windows to open browser draft storage.'));
        request.onsuccess=()=>{const db=request.result;db.onversionchange=()=>{db.close();opening=null;};resolve(db);};
    });
    void opening.catch(()=>{opening=null;});return opening;
}
const recordKey=(identity:DocumentIdentity,writerId:string)=>`${documentKey(identity)}\u0000${writerId}`;
export const documentJournal: DocumentJournal = {
    async load(identity){
        const db=await database();return new Promise((resolve,reject)=>{
            const tx=db.transaction('documents','readonly');const request=tx.objectStore('documents').index('document').getAll(documentKey(identity));
            request.onerror=()=>reject(request.error);
            tx.onabort=()=>reject(tx.error);
            tx.oncomplete=()=>resolve(request.result.map(({record}:{record:DocumentRecord})=>record));
        });
    },
    async save(record){
        const db=await database();return new Promise((resolve,reject)=>{
            const tx=db.transaction('documents','readwrite',{durability:'strict'});
            tx.objectStore('documents').put({key:recordKey(record.identity,record.writerId),document:documentKey(record.identity),record});
            tx.oncomplete=()=>resolve();tx.onabort=()=>reject(tx.error??new Error('Browser draft transaction was aborted.'));tx.onerror=()=>reject(tx.error);
        });
    },
    async remove(writerId,identity){
        const db=await database();return new Promise((resolve,reject)=>{
            const tx=db.transaction('documents','readwrite',{durability:'strict'});tx.objectStore('documents').delete(recordKey(identity,writerId));
            tx.oncomplete=()=>resolve();tx.onabort=()=>reject(tx.error);tx.onerror=()=>reject(tx.error);
        });
    },
};

export interface PendingFileOperation {key:string;origin:string;channel:string;cellId:string;operation:import('./fileProtocol').FileOperation;renames?:{source:string;target:string;writerId:string;capturedEditSeq:number}[];move?:{operation_id:string;source_cell_id:string;target_cell_id:string;files:{source_path:string;target_path:string;source_revision:number;target_revision:number}[];writer_id:string;edit_seq:number}}
const operationListeners=new Set<()=>void>();
const notifyOperations=()=>{for(const listener of operationListeners)listener();};
export const operationJournal={
    subscribe(listener:()=>void):()=>void{operationListeners.add(listener);return()=>{operationListeners.delete(listener);};},
    async load():Promise<PendingFileOperation[]>{const db=await database();return new Promise((resolve,reject)=>{const tx=db.transaction('operations','readonly');const request=tx.objectStore('operations').getAll();tx.oncomplete=()=>resolve(request.result);tx.onabort=()=>reject(tx.error);});},
    async save(record:PendingFileOperation):Promise<void>{const db=await database();await new Promise<void>((resolve,reject)=>{const tx=db.transaction('operations','readwrite',{durability:'strict'});tx.objectStore('operations').put(record);tx.oncomplete=()=>resolve();tx.onabort=()=>reject(tx.error);});notifyOperations();},
    async remove(key:string):Promise<void>{const db=await database();await new Promise<void>((resolve,reject)=>{const tx=db.transaction('operations','readwrite',{durability:'strict'});tx.objectStore('operations').delete(key);tx.oncomplete=()=>resolve();tx.onabort=()=>reject(tx.error);});notifyOperations();},
};

export const codecJournal={
    async load(hash:string):Promise<import('./fileSnapshotCodec').JournaledCodec|undefined>{const db=await database();return new Promise((resolve,reject)=>{const tx=db.transaction('codecs','readonly'),request=tx.objectStore('codecs').get(hash);tx.oncomplete=()=>{const record=request.result as import('./fileSnapshotCodec').JournaledCodec|undefined;resolve(record?.shape===CODEC_RECORD_SHAPE?record:undefined);};tx.onabort=()=>reject(tx.error);});},
    async save(codec:import('./fileSnapshotCodec').JournaledCodec):Promise<void>{const db=await database();return new Promise((resolve,reject)=>{const tx=db.transaction('codecs','readwrite',{durability:'strict'});tx.objectStore('codecs').put(codec);tx.oncomplete=()=>resolve();tx.onabort=()=>reject(tx.error);});},
};
