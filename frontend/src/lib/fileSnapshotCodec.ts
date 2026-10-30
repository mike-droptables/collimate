import {codecJournal} from './fileDocumentStorage';
import {getApiUrl} from './api/client';
export interface RendererSnapshotCodec {id:string;version:number;implementationHash:string;source:string}
export interface RendererRecoverySnapshot {codecHash:string;snapshotId:string;payload:unknown}
export interface RendererDocumentSnapshot {content:string;recovery?:RendererRecoverySnapshot;editSeq:number;materializedSeq:number}
interface ApprovedCodec extends RendererSnapshotCodec {approved:true}
const known=new Map<string,ApprovedCodec>();
let manifest:Promise<{codecs:Omit<RendererSnapshotCodec,'source'>[]}>|null=null;
const digest=async(source:string)=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(source)))).map(byte=>byte.toString(16).padStart(2,'0')).join('');
export async function registerSnapshotCodec(codec:RendererSnapshotCodec):Promise<void>{
    if(await digest(codec.source)!==codec.implementationHash)throw new Error('Snapshot codec implementation hash does not match its source.');
    const cached=await codecJournal.load(codec.implementationHash) as ApprovedCodec|undefined;
    if(cached?.approved&&cached.id===codec.id&&cached.version===codec.version&&cached.source===codec.source){known.set(codec.implementationHash,cached);return;}
    manifest??=fetch(`${getApiUrl()}/sdk/snapshot-codecs.json`,{cache:'no-store'}).then(async response=>{if(!response.ok)throw new Error(`Snapshot codec trust manifest: HTTP ${response.status}`);return response.json();});
    let trusted:{codecs:Omit<RendererSnapshotCodec,'source'>[]};
    try{trusted=await manifest;}catch(error){manifest=null;throw error;}
    if(!trusted.codecs.some(item=>item.id===codec.id&&item.version===codec.version&&item.implementationHash===codec.implementationHash))throw new Error('Snapshot codec is not approved by this installation.');
    const record:ApprovedCodec={...codec,approved:true};await codecJournal.save(record);known.set(codec.implementationHash,record);
}
async function approvedCodec(hash:string):Promise<ApprovedCodec>{
    const cached=known.get(hash);if(cached)return cached;
    const record=await codecJournal.load(hash) as ApprovedCodec|undefined;
    if(!record?.approved||await digest(record.source)!==hash)throw new Error('A trusted decoder for this browser draft is unavailable.');
    known.set(hash,record);return record;
}
interface Decoder {worker:Worker;requests:Map<string,{resolve:(body:string)=>void;reject:(error:Error)=>void}>}
const decoders=new Map<string,Decoder>();
async function decoder(hash:string):Promise<Decoder>{
    const cached=decoders.get(hash);if(cached)return cached;
    const codec=await approvedCodec(hash);
    const loaded=decoders.get(hash);if(loaded)return loaded;
    const url=URL.createObjectURL(new Blob([codec.source],{type:'application/javascript'}));
    let worker:Worker;try{worker=new Worker(url);}finally{URL.revokeObjectURL(url);}
    const requests:Decoder['requests']=new Map();const result={worker,requests};
    worker.onmessage=event=>{const message=event.data;const request=requests.get(message?.requestId);if(!request)return;requests.delete(message.requestId);if(message.type==='materialized'&&typeof message.body==='string')request.resolve(message.body);else request.reject(new Error(String(message.error??'Snapshot decoder returned invalid bytes.')));};
    worker.onerror=event=>{decoders.delete(hash);for(const request of requests.values())request.reject(new Error(event.message||'Snapshot decoder failed.'));requests.clear();worker.terminate();};
    decoders.set(hash,result);return result;
}
export async function materializeSnapshot(snapshot:RendererRecoverySnapshot):Promise<string>{
    const target=await decoder(snapshot.codecHash),requestId=crypto.randomUUID();
    return new Promise((resolve,reject)=>{target.requests.set(requestId,{resolve,reject});target.worker.postMessage({type:'materialize',requestId,payload:snapshot.payload});});
}
