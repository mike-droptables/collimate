import {CODEC_RECORD_SHAPE,codecJournal} from './fileDocumentStorage';
import {getApiUrl} from './api/client';
export interface RendererSnapshotCodec {id:string;version:number;implementationHash:string;source:string}
export interface RendererRecoverySnapshot {codecHash:string;snapshotId:string;payload:unknown}
export interface RendererDocumentSnapshot {content:string;recovery?:RendererRecoverySnapshot;editSeq:number;materializedSeq:number}
/** A codec as the browser journals it: its record ``shape``, its source, for
 *  recovering drafts after a reload, and the channels whose trust list named
 *  it when it last registered there (the fallback while that list cannot be
 *  fetched). */
export type JournaledCodec=RendererSnapshotCodec&{shape:typeof CODEC_RECORD_SHAPE;channels:string[]};
type TrustList=Omit<RendererSnapshotCodec,'source'>[];
// A channel trusts the SDK runtime's codecs and those of the renderers on its
// Capabilities: GET /api/channels/{ch}/snapshot-codecs, asked once per page
// and again after a refusal, so a renderer added meanwhile registers.
const lists=new Map<string,Promise<TrustList>>();
const known=new Map<string,RendererSnapshotCodec>();
const key=(channel:string,hash:string)=>`${channel}\0${hash}`;
const digest=async(source:string)=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(source)))).map(byte=>byte.toString(16).padStart(2,'0')).join('');
/** The channel's trust list, or null when it cannot be fetched. */
async function trustList(channel:string):Promise<TrustList|null>{
    let list=lists.get(channel);
    if(!list){
        list=fetch(`${getApiUrl()}/channels/${encodeURIComponent(channel)}/snapshot-codecs`,{cache:'no-store'}).then(async response=>{if(!response.ok)throw new Error(`Snapshot codec trust list: HTTP ${response.status}`);return (await response.json()).codecs as TrustList;});
        lists.set(channel,list);
    }
    try{return await list;}catch{lists.delete(channel);return null;}
}
async function journal(codec:RendererSnapshotCodec,channel:string,trusted:boolean):Promise<void>{
    const record=await codecJournal.load(codec.implementationHash);
    const others=(record?.source===codec.source?record.channels:[]).filter(item=>item!==channel);
    if(trusted)await codecJournal.save({...codec,shape:CODEC_RECORD_SHAPE,channels:[...others,channel]});
    else if(record&&record.channels.includes(channel))await codecJournal.save({...record,channels:others});
}
/** Register a renderer's codec for its page in ``channel``: its source must
 *  hash to its implementation hash, and the channel's trust list must name it. */
export async function registerSnapshotCodec(channel:string,codec:RendererSnapshotCodec):Promise<void>{
    if(await digest(codec.source)!==codec.implementationHash)throw new Error('Snapshot codec implementation hash does not match its source.');
    const list=await trustList(channel);
    if(list===null){
        const record=await codecJournal.load(codec.implementationHash);
        if(record&&record.channels.includes(channel)&&record.id===codec.id&&record.version===codec.version&&record.source===codec.source){known.set(key(channel,codec.implementationHash),codec);return;}
        throw new Error('This channel’s snapshot codec trust list is unreachable.');
    }
    if(!list.some(item=>item.id===codec.id&&item.version===codec.version&&item.implementationHash===codec.implementationHash)){
        lists.delete(channel);await journal(codec,channel,false);
        throw new Error('Snapshot codec is not trusted in this channel: add its renderer to this channel’s Capabilities.');
    }
    await journal(codec,channel,true);known.set(key(channel,codec.implementationHash),codec);
}
async function trustedCodec(channel:string,hash:string):Promise<RendererSnapshotCodec>{
    const cached=known.get(key(channel,hash));if(cached)return cached;
    const unavailable=new Error('A trusted decoder for this browser draft is unavailable.');
    const record=await codecJournal.load(hash);
    if(!record||await digest(record.source)!==hash)throw unavailable;
    const list=await trustList(channel);
    if(list===null?!record.channels.includes(channel):!list.some(item=>item.implementationHash===hash))throw unavailable;
    const codec={id:record.id,version:record.version,implementationHash:hash,source:record.source};
    known.set(key(channel,hash),codec);return codec;
}
interface Decoder {worker:Worker;requests:Map<string,{resolve:(body:string)=>void;reject:(error:Error)=>void}>}
const decoders=new Map<string,Decoder>();
async function decoder(channel:string,hash:string):Promise<Decoder>{
    const codec=await trustedCodec(channel,hash);
    const loaded=decoders.get(hash);if(loaded)return loaded;
    const url=URL.createObjectURL(new Blob([codec.source],{type:'application/javascript'}));
    let worker:Worker;try{worker=new Worker(url);}finally{URL.revokeObjectURL(url);}
    const requests:Decoder['requests']=new Map();const result={worker,requests};
    worker.onmessage=event=>{const message=event.data;const request=requests.get(message?.requestId);if(!request)return;requests.delete(message.requestId);if(message.type==='materialized'&&typeof message.body==='string')request.resolve(message.body);else request.reject(new Error(String(message.error??'Snapshot decoder returned invalid bytes.')));};
    worker.onerror=event=>{decoders.delete(hash);for(const request of requests.values())request.reject(new Error(event.message||'Snapshot decoder failed.'));requests.clear();worker.terminate();};
    decoders.set(hash,result);return result;
}
/** The text of a recovery snapshot of a document in ``channel``, derived by
 *  a codec that channel trusts. */
export async function materializeSnapshot(channel:string,snapshot:RendererRecoverySnapshot):Promise<string>{
    const target=await decoder(channel,snapshot.codecHash),requestId=crypto.randomUUID();
    return new Promise((resolve,reject)=>{target.requests.set(requestId,{resolve,reject});target.worker.postMessage({type:'materialize',requestId,payload:snapshot.payload});});
}
