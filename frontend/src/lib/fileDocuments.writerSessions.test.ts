import {createHash,webcrypto} from 'node:crypto';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import type {DocumentRecord} from './fileDocument';
import type {FileDescriptor,FileMutationResult,FileOperation} from './fileProtocol';
import type {VirtualFile} from './types';

const test=vi.hoisted(()=>({
    records:new Map<string,DocumentRecord>(),current:new Map<string,FileDescriptor>(),
    bodies:new Map<string,string>(),highwater:new Map<string,number>(),receipts:new Map<string,FileMutationResult>(),
    sent:[] as FileOperation[],loseResponse:false,
}));
const windowId='f81a72dd-0926-4810-9e87-5d72576e62f0';
const channel='d5cdd17a-cd92-450e-9782-90e97c2f21b2',cell='cell';
const key=(record:Pick<DocumentRecord,'identity'|'writerId'>)=>JSON.stringify([record.identity,record.writerId]);
vi.mock('./fileDocumentStorage',()=>({documentJournal:{
    load:async(identity:DocumentRecord['identity'])=>[...test.records.values()].filter(record=>JSON.stringify(record.identity)===JSON.stringify(identity)),
    save:async(record:DocumentRecord)=>{test.records.set(key(record),structuredClone(record));},
    remove:async(writerId:string,identity:DocumentRecord['identity'])=>{test.records.delete(key({writerId,identity}));},
}}));
vi.mock('./fileCommands',()=>({applyMutationResult:()=>{}}));
vi.mock('@/stores/useCollimateStore',()=>({useCollimateStore:{getState:()=>({})}}));
vi.mock('./fileBodies',()=>({
    descriptorOf:(file:VirtualFile)=>({path:file.name,...file}),
    readPinnedText:async(_channel:string,_cell:string,file:FileDescriptor)=>test.bodies.get(file.body_hash!)!,
}));
vi.mock('./api/client',()=>({getApiUrl:()=> 'https://session.test/api',requestJson:async(_url:string,method:string,operation?:FileOperation)=>{
    if(method==='GET')return {conflicts:[]};
    test.sent.push(structuredClone(operation!));
    const result=apply(operation!);
    if(test.loseResponse){test.loseResponse=false;throw new Error('lost accepted response');}
    return result;
}}));

// Model the server's retained (path, writer) highwater and exact receipts,
// independently of current descriptor provenance and browser journal cleanup.
function apply(operation:FileOperation):FileMutationResult{
    const existing=test.receipts.get(operation.operation_id);if(existing)return existing;
    const stale=operation.mutations.some(mutation=>(test.highwater.get(JSON.stringify([mutation.path,mutation.writer_id]))??0)>=mutation.edit_seq);
    const conflict=operation.mutations.some(mutation=>mutation.base_revision!==(test.current.get(mutation.path)?.file_revision??0));
    const result:FileMutationResult={operation_id:operation.operation_id,outcome:stale?'superseded':conflict?'conflict':'accepted',files:[],conflicts:[]};
    if(result.outcome==='accepted')for(const mutation of operation.mutations){
        const body=mutation.content_text??test.bodies.get(mutation.blob_hash!)??'';
        const hash=mutation.kind==='delete'?null:createHash('sha256').update(body).digest('hex');
        if(hash)test.bodies.set(hash,body);
        const file:FileDescriptor={path:mutation.path,file_revision:mutation.base_revision+1,body_hash:hash,
            deleted:mutation.kind==='delete',encoding:'utf-8',language:'text',writer_id:mutation.writer_id,edit_seq:mutation.edit_seq};
        test.current.set(file.path,file);test.highwater.set(JSON.stringify([file.path,file.writer_id]),file.edit_seq);result.files.push(file);
    }
    else result.files=operation.mutations.map(mutation=>test.current.get(mutation.path)!);
    test.receipts.set(operation.operation_id,result);return result;
}
function host(path:string,body?:string):FileDescriptor{
    apply({operation_id:crypto.randomUUID(),mutations:[{path,writer_id:crypto.randomUUID(),edit_seq:1,
        base_revision:test.current.get(path)?.file_revision??0,kind:body===undefined?'delete':'put',content_text:body}],resolves:[]});
    return test.current.get(path)!;
}
function file(path:string):VirtualFile {const {path:name,...metadata}=test.current.get(path)!;return {name,...metadata};}
async function registry(){vi.resetModules();return import('./fileDocuments');}
beforeEach(()=>{
    vi.useFakeTimers();vi.stubGlobal('crypto',webcrypto);vi.stubGlobal('BroadcastChannel',undefined);
    const storage=new Map<string,string>();
    vi.stubGlobal('window',{location:{href:'https://session.test/'}});
    vi.stubGlobal('sessionStorage',{getItem:(name:string)=>storage.get(name)??null,setItem:(name:string,value:string)=>storage.set(name,value)});
    sessionStorage.setItem('collimate.documentWriter',windowId);
    for(const map of [test.records,test.current,test.bodies,test.highwater,test.receipts])map.clear();
    test.sent=[];test.loseResponse=false;host('old.txt','initial');
});
afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals();});

it('clean accepted save then same-tab reload allocates a new writer and accepts the next edit',async()=>{
    let api=await registry();const first=await api.acquireFileDocument(channel,cell,file('old.txt'));
    first.edit('saved before reload');await first.flush();expect(test.records.size).toBe(0);
    const writer=first.writerId;expect(test.highwater.get(JSON.stringify(['old.txt',writer]))).toBe(1);
    api=await registry();const reopened=await api.acquireFileDocument(channel,cell,file('old.txt'));
    expect(reopened.writerId).not.toBe(writer);expect(reopened.getSnapshot().editSeq).toBe(0);
    reopened.edit('saved after reload');await reopened.flush();
    expect(reopened.getSnapshot()).toMatchObject({status:'Saved',savedSeq:1});
    expect(test.bodies.get(test.current.get('old.txt')!.body_hash!)).toBe('saved after reload');
});

it('pending same-tab recovery keeps its exact prior writer and accepted-but-lost operation',async()=>{
    let api=await registry();const first=await api.acquireFileDocument(channel,cell,file('old.txt'));
    test.loseResponse=true;first.edit('uncertain save');await expect(first.flush()).rejects.toThrow('lost accepted response');
    const exact=structuredClone(test.sent[0]);api=await registry();
    const recovered=await api.acquireFileDocument(channel,cell,file('old.txt'));
    expect(recovered.writerId).toBe(first.writerId);expect(recovered.getSnapshot()).toMatchObject({body:'uncertain save',editSeq:1,savedSeq:0});
    await recovered.flush();expect(test.sent[1]).toEqual(exact);
    expect(recovered.getSnapshot()).toMatchObject({status:'Saved',savedSeq:1});
});

it('rename retains its writer while a recreated source allocates an independent sequence domain',async()=>{
    const api=await registry();const owner=await api.acquireFileDocument(channel,cell,file('old.txt'));
    owner.edit('first saved text');await owner.flush();const writer=owner.writerId;
    const target=host('new.txt','first saved text');host('old.txt');
    await api.retargetFileDocument(channel,cell,'old.txt',target,owner.getSnapshot().editSeq);
    expect(owner.writerId).toBe(writer);
    host('old.txt','recreated');const recreated=await api.acquireFileDocument(channel,cell,file('old.txt'));
    expect(recreated.writerId).not.toBe(writer);recreated.edit('new source edit');await recreated.flush();
    expect(recreated.getSnapshot().status).toBe('Saved');expect(owner.getSnapshot().body).toBe('first saved text');
});

it('rename into a previously edited and deleted target preserves the source writer without target highwater reuse',async()=>{
    const api=await registry();host('new.txt','old target');
    const oldTarget=await api.acquireFileDocument(channel,cell,file('new.txt'));
    for(const body of ['target edit one','target edit two','target edit three']){oldTarget.edit(body);await oldTarget.flush();}
    host('new.txt');const source=await api.acquireFileDocument(channel,cell,file('old.txt'));
    source.edit('source edit');await source.flush();const writer=source.writerId;
    const target=host('new.txt','source edit');host('old.txt');
    await api.retargetFileDocument(channel,cell,'old.txt',target,source.getSnapshot().editSeq);
    expect(source.writerId).toBe(writer);expect(source.writerId).not.toBe(oldTarget.writerId);
    source.edit('new target edit');await source.flush();
    expect(source.getSnapshot()).toMatchObject({status:'Saved',savedSeq:2});
    expect(test.highwater.get(JSON.stringify(['new.txt',oldTarget.writerId]))).toBe(3);
    expect(test.bodies.get(test.current.get('new.txt')!.body_hash!)).toBe('new target edit');
});
