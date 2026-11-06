// @vitest-environment jsdom
import {act,renderHook,waitFor} from '@testing-library/react';
import {beforeEach,describe,expect,it,vi} from 'vitest';
import {FileDocument,type DocumentJournal} from '@/lib/fileDocument';
import type {FileOperation,FileMutationResult} from '@/lib/fileProtocol';
import {useCollimateStore} from '@/stores/useCollimateStore';
import {useFileOperations} from '@/hooks/useFileOperations';
import {buildFileTree} from '@/lib/fileTree';
import {replaceCellFiles} from '@/lib/fileCommands';
const h=vi.hoisted(()=>({owner:null as FileDocument|null,mutate:vi.fn(),request:vi.fn(),save:vi.fn(async(_record:unknown)=>{}),remove:vi.fn(async()=>{}),flush:vi.fn(async()=>{}),barrier:vi.fn(async()=>{})}));
vi.mock('@/lib/fileDocuments',()=>({getFileDocument:(_channel:string,_cell:string,path:string)=>h.owner?.identity.path===path?h.owner:undefined,retargetFileDocument:async(_channel:string,_cell:string,_source:string,target:import('@/lib/fileProtocol').FileDescriptor,seq:number)=>{if(!h.owner)return false;await h.owner.moveTo(target,seq);return true;},flushFileDocuments:h.flush,mutateFiles:h.mutate,observeCellDocuments:()=>{},hasDirtyDocuments:()=>false}));
vi.mock('@/lib/fileDocumentStorage',()=>({operationJournal:{save:h.save,remove:h.remove},documentJournal:{remove:h.remove}}));
vi.mock('@/lib/cellSaveBarrier',()=>({flushCellSaves:h.barrier}));
vi.mock('@/lib/rendererFlush',()=>({flushRendererEditors:h.barrier}));
vi.mock('@/lib/api/client',async original=>({...await original<typeof import('@/lib/api/client')>(),getApiUrl:()=>'/api',requestJson:h.request}));
vi.mock('@/lib/fileTransfer',async original=>({...await original<typeof import('@/lib/fileTransfer')>(),fetchTextBodies:async()=>new Map([['old.txt','server body']])}));
const metadata={name:'old.txt',language:'text',file_revision:7,body_hash:'oldhash',encoding:'utf-8' as const,writer_id:'server',edit_seq:1};
let confirm:ReturnType<typeof vi.fn<(opts:{title:string;body:string})=>Promise<boolean>>>,journal:DocumentJournal;
beforeEach(async()=>{
 vi.clearAllMocks();const cell={id:'cell',name:'Cell',files:[metadata]},channel={id:'channel',name:'channel',nav_stacks:[],main_stacks:[{id:'stack',name:'',cells:[cell]}],nav_selection_id:null,main_selection_id:'stack'};
 confirm=vi.fn(async()=>true);useCollimateStore.setState({activeChannel:channel,state:{active_channel_id:'channel',channels:[channel]},cellCache:{cell},openConfirm:confirm});
 journal={load:async()=>[],save:vi.fn(async(_record:unknown)=>{}),remove:vi.fn(async()=>{})};
 h.owner=new FileDocument({origin:location.origin,channel:'channel',cellId:'cell',path:'old.txt'},'editor',journal,vi.fn(),async()=> 'server body',100000);
 await h.owner.initialize({path:metadata.name,...metadata,deleted:false});h.owner.edit('last unsaved keystroke');
 h.request.mockResolvedValue({files:[]});h.mutate.mockImplementation(async(_channel:string,_cell:string,operation:FileOperation):Promise<FileMutationResult>=>({operation_id:operation.operation_id,outcome:'accepted',files:operation.mutations.map(m=>({path:m.path,file_revision:m.base_revision+1,body_hash:m.kind==='delete'?null:'newhash',deleted:m.kind==='delete',writer_id:m.writer_id,edit_seq:m.edit_seq,language:m.language??'text',encoding:m.encoding??'utf-8'})),conflicts:[]}));
});
const hook=()=>renderHook(()=>useFileOperations({files:[metadata],channel:'channel',cellId:'cell',readOnly:false,onUpdateFiles:(update,options)=>replaceCellFiles('channel','cell',update(useCollimateStore.getState().cellCache.cell.files),useCollimateStore.getState().cellCache.cell.files,options?.renames)}));
describe('CellEditor atomic dirty rename boundary',()=>{
 it('captures the revision after an inflight ACK without a separate save of later source edits',async()=>{
  let accept!:(receipt:FileMutationResult)=>void;
  const pending=new Promise<FileMutationResult>(resolve=>{accept=resolve;});
  const send=vi.fn<(_operation:FileOperation)=>Promise<FileMutationResult>>(()=>pending);
  h.owner=new FileDocument({origin:location.origin,channel:'channel',cellId:'cell',path:'old.txt'},'editor',journal,send,async()=> 'server body',100000);
  await h.owner.initialize({path:metadata.name,...metadata,deleted:false});h.owner.edit('first edit');
  const saving=h.owner.flush();await waitFor(()=>expect(send).toHaveBeenCalledOnce());
  h.owner.edit('later edit for destination');
  const {result}=hook();const rename=result.current.handleRename(buildFileTree([metadata])['old.txt'],'new.txt');
  await waitFor(()=>expect(h.owner!.getSnapshot().structuralPending).toBe(true));
  const operation=send.mock.calls[0][0] as FileOperation;
  accept({operation_id:operation.operation_id,outcome:'accepted',files:[{path:'old.txt',file_revision:8,body_hash:'changed',deleted:false,writer_id:'editor',edit_seq:1,language:'text',encoding:'utf-8'}],conflicts:[]});
  await saving;await rename;
  expect(send).toHaveBeenCalledOnce();
  expect(h.mutate.mock.calls[0][2].mutations).toEqual([expect.objectContaining({path:'new.txt',content_text:'later edit for destination'}),expect.objectContaining({path:'old.txt',kind:'delete',base_revision:8})]);
 });
 it('maps identical accepted hashes by path when a folder contains a dirty owner',async()=>{
  const files=[{...metadata,name:'docs/b.txt'},{...metadata,name:'docs/a.txt'}];
  useCollimateStore.getState().patchCell('cell',{files});
  h.owner=new FileDocument({origin:location.origin,channel:'channel',cellId:'cell',path:'docs/a.txt'},'editor',journal,vi.fn(),async()=> 'server body',100000);
  await h.owner.initialize({...metadata,path:'docs/a.txt',deleted:false});h.owner.edit('dirty a only');
  const {result}=renderHook(()=>useFileOperations({files,channel:'channel',cellId:'cell',readOnly:false,onUpdateFiles:(update,options)=>replaceCellFiles('channel','cell',update(files),files,options?.renames)}));
  await result.current.handleRename(buildFileTree(files).docs,'renamed');
  const mutations=h.mutate.mock.calls[0][2].mutations;
  expect(mutations).toEqual([expect.objectContaining({path:'renamed/b.txt',blob_hash:'oldhash'}),expect.objectContaining({path:'renamed/a.txt',content_text:'dirty a only'}),expect.objectContaining({path:'docs/b.txt',kind:'delete'}),expect.objectContaining({path:'docs/a.txt',kind:'delete'})]);
 });
 it('confirms and saves the latest local bytes and source deletion in one group',async()=>{
  const {result}=hook();await act(async()=>{await result.current.handleRename(buildFileTree([metadata])['old.txt'],'new.txt');});
  expect(confirm).toHaveBeenCalledWith(expect.objectContaining({title:'Save your edits and rename?',confirmLabel:'Save and rename'}));
  expect(h.barrier).toHaveBeenCalledWith('cell');expect(h.flush).not.toHaveBeenCalled();
  expect(h.mutate.mock.calls[0][2].mutations).toEqual([expect.objectContaining({path:'new.txt',content_text:'last unsaved keystroke',base_revision:0}),expect.objectContaining({path:'old.txt',kind:'delete',base_revision:7})]);
  expect(journal.remove).toHaveBeenCalled();expect(h.owner!.getSnapshot().savedSeq).toBe(1);
 });
 it('cancel leaves the draft and identity untouched',async()=>{
  confirm.mockResolvedValue(false);const {result}=hook();await act(async()=>{await result.current.handleRename(buildFileTree([metadata])['old.txt'],'new.txt');});
  expect(h.mutate).not.toHaveBeenCalled();expect(h.owner!.getSnapshot().body).toBe('last unsaved keystroke');expect(journal.remove).not.toHaveBeenCalled();
 });
 it('uncertain response restores editing and retains both recovery records and exact operation',async()=>{
  h.mutate.mockRejectedValue(new Error('lost response'));const {result}=hook();
  await act(async()=>{await expect(result.current.handleRename(buildFileTree([metadata])['old.txt'],'new.txt')).rejects.toThrow('lost response');});
  expect(h.remove).not.toHaveBeenCalled();expect(journal.remove).not.toHaveBeenCalled();
  expect(h.owner!.getSnapshot()).toMatchObject({body:'last unsaved keystroke',structuralPending:false});
  expect(h.save.mock.calls[0][0]).toMatchObject({renames:[{source:'old.txt',target:'new.txt',writerId:'editor'}]});
 });
});
