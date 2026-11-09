import {createHash} from 'node:crypto';
import {beforeEach,describe,expect,it,vi} from 'vitest';
import {FileDocument,type DocumentJournal,type DocumentRecord} from '../fileDocument';
import type {FileDescriptor,FileOperation} from '../fileProtocol';
import {useCollimateStore} from '@/stores/useCollimateStore';
import {saveSiblingFileInCell} from '../siblingWrite';
const h=vi.hoisted(()=>({owner:null as FileDocument|null,acquire:vi.fn(),revision:vi.fn(async()=>0)}));
vi.mock('../fileDocuments',()=>({acquireFileDocument:h.acquire,hasDirtyDocuments:()=>false}));
vi.mock('../fileCommands',()=>({expectedFileRevision:h.revision,textFile:(name:string,content:string)=>({name,content,language:'plaintext',file_revision:0,body_hash:null,encoding:'utf-8',writer_id:'',edit_seq:0})}));
const metadata:FileDescriptor={path:'comments.yaml',file_revision:1,body_hash:'base',language:'plaintext',encoding:'utf-8',deleted:false,writer_id:'server',edit_seq:1};
let journal:DocumentJournal,records:Map<string,DocumentRecord>,send:ReturnType<typeof vi.fn<(operation:FileOperation)=>Promise<import('../fileProtocol').FileMutationResult>>>;
const create=async(file=metadata)=>{const owner=new FileDocument({origin:'https://test',channel:'channel',cellId:'cell',path:file.path},'writer',journal,send,async()=> 'accepted base',100000);await owner.initialize(file);return owner;};
beforeEach(async()=>{
 vi.clearAllMocks();records=new Map();journal={load:async()=>[...records.values()],save:async record=>{records.set(record.writerId,structuredClone(record));},remove:async writer=>{records.delete(writer);}};
 send=vi.fn(async op=>({operation_id:op.operation_id,outcome:'accepted',files:op.mutations.map(m=>({...metadata,path:m.path,file_revision:m.base_revision+1,writer_id:m.writer_id,edit_seq:m.edit_seq,body_hash:createHash('sha256').update(m.content_text??'').digest('hex')})),conflicts:[]}));
 h.owner=await create();h.acquire.mockImplementation(async()=>h.owner);
 const {path,...info}=metadata,cell={id:'cell',name:'Cell',files:[{name:path,...info}]};
 const channel={id:'channel',name:'channel',nav_stacks:[],main_stacks:[{id:'stack',name:'',cells:[cell]}],nav_selection_id:null,main_selection_id:'stack'};
 useCollimateStore.setState({cellCache:{cell},activeChannel:channel,state:{active_channel_id:'channel',channels:[channel]}});
});
describe('renderer sibling document durability',()=>{
 it('hands edits to the same document owner and waits for its accepted receipt',async()=>{
  expect(await saveSiblingFileInCell('cell','comments.yaml','current comment')).toEqual({ok:true});
  expect(send.mock.calls[0][0].mutations[0]).toMatchObject({path:'comments.yaml',content_text:'current comment',base_revision:1,edit_seq:1});
 });
 it('preserves a submitted semantic comment on refused save and recovers it before accepted server bytes',async()=>{
  send.mockRejectedValue(new Error('storage refused'));
  expect(await saveSiblingFileInCell('cell','comments.yaml','last submitted comment')).toEqual({ok:false,reason:'refused'});
  const exact=records.get('writer')!.operation;h.owner=await create();
  expect(h.owner.getSnapshot().body).toBe('last submitted comment');expect(records.get('writer')?.operation).toEqual(exact);
 });
 it('journals newer comments even while an older exact operation is refused',async()=>{
  send.mockRejectedValue(new Error('offline'));
  await saveSiblingFileInCell('cell','comments.yaml','one');await saveSiblingFileInCell('cell','comments.yaml','two');
  expect(records.get('writer')).toMatchObject({body:'two',editSeq:2,operation:{mutations:[{content_text:'one',edit_seq:1}]}});
 });
 it('creates an absent zero-byte text sibling using its retained tombstone revision',async()=>{
  h.revision.mockResolvedValueOnce(7);h.owner=await create({...metadata,path:'empty.txt',file_revision:7,body_hash:null,deleted:true});
  expect(await saveSiblingFileInCell('cell','empty.txt','')).toEqual({ok:true});
  expect(h.acquire).toHaveBeenCalledWith('channel','cell',expect.objectContaining({name:'empty.txt',deleted:true,file_revision:7}));
  expect(send.mock.calls[0][0].mutations[0]).toMatchObject({path:'empty.txt',content_text:'',base_revision:7});
 });
 it('hard view readonly denies interactions before document acquisition',async()=>{
  expect(await saveSiblingFileInCell('cell','comments.yaml','refused',true)).toEqual({ok:false,reason:'read-only'});expect(h.acquire).not.toHaveBeenCalled();
 });
 it('leaves run-sidecar authorization to the server semantic policy and preserves refused proposals',async()=>{
  h.owner=await create({...metadata,path:'_runs/finished/ui.chat'});send.mockRejectedValueOnce(new Error('run-owned primary file is readonly'));
  expect(await saveSiblingFileInCell('cell','_runs/finished/ui.chat','arbitrary primary put')).toEqual({ok:false,reason:'refused'});expect(records.get('writer')?.body).toBe('arbitrary primary put');
 });
 it('reports missing cells and binary text writes without creating a draft',async()=>{
  expect(await saveSiblingFileInCell('missing','comments.yaml','x')).toEqual({ok:false,reason:'unavailable'});
  const cell=useCollimateStore.getState().cellCache.cell;useCollimateStore.getState().patchCell('cell',{files:cell.files.map(file=>({...file,encoding:'binary'}))});
  expect(await saveSiblingFileInCell('cell','comments.yaml','x')).toEqual({ok:false,reason:'refused'});expect(h.acquire).not.toHaveBeenCalled();
 });
});
