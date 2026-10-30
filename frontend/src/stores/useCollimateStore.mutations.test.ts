// @vitest-environment jsdom
import 'fake-indexeddb/auto';
import {beforeEach,expect,it,vi} from 'vitest';
import {waitFor} from '@testing-library/react';
import {fileFixture} from '@/test/fileFixtures';
import type {FileOperation,FileMutationResult} from '@/lib/fileProtocol';
vi.mock('@/lib/api/client',async original=>({...await original<Record<string,unknown>>(),requestJson:vi.fn()}));
import {requestJson} from '@/lib/api/client';
import {useCollimateStore} from './useCollimateStore';
import type {Cell,Channel} from '@/lib/types';
const cell:Cell={id:'cut-cell',name:'',files:Array.from({length:500},(_,i)=>fileFixture(`cut-${i}.txt`,'body'))};
const channel:Channel={id:'cut-channel',name:'cut',nav_stacks:[],main_stacks:[{id:'cut-stack',name:'',cells:[cell]}],nav_selection_id:null,main_selection_id:'cut-stack'};
beforeEach(()=>{vi.clearAllMocks();vi.mocked(requestJson).mockImplementation(async(_path,_method,input)=>{
 const operation=input as FileOperation;
 const result:FileMutationResult={operation_id:operation.operation_id,outcome:'accepted',files:operation.mutations.map(mutation=>({path:mutation.path,file_revision:mutation.base_revision+1,body_hash:null,deleted:true,writer_id:mutation.writer_id,edit_seq:mutation.edit_seq,language:'plaintext',encoding:'utf-8'})),conflicts:[]};
 return result;
});useCollimateStore.setState({state:null,activeChannel:null,cellCache:{},runs:{}});useCollimateStore.getState().applyServerState({active_channel_id:channel.id,channels:[channel]});});
it('a500-file cut publishes one conditional mutation group and clears metadata only after acceptance',async()=>{
 expect(await useCollimateStore.getState().applyCellFiles(cell.id,[])).toBe(true);
 expect(requestJson).toHaveBeenCalledOnce();
 const [path,method,input]=vi.mocked(requestJson).mock.calls[0];
 expect([path,method]).toEqual(['/channels/cut-channel/cells/cut-cell/files/mutate','POST']);
 const operation=input as FileOperation;expect(operation.mutations).toHaveLength(500);
 expect(operation.mutations.every(mutation=>mutation.kind==='delete'&&mutation.base_revision===1)).toBe(true);
 expect(useCollimateStore.getState().cellCache[cell.id].files).toEqual([]);
});
it('a refused conditional save preserves newer accepted metadata',async()=>{
 let refuse!:(reason:Error)=>void;
 vi.mocked(requestJson).mockImplementationOnce(()=>new Promise((_resolve,reject)=>{refuse=reject}));
 const saving=useCollimateStore.getState().applyCellFiles(cell.id,[]);
 await waitFor(()=>expect(requestJson).toHaveBeenCalledOnce());
 const newer=[fileFixture('new.txt','newer')];useCollimateStore.getState().patchCell(cell.id,{files:newer});
 refuse(new Error('controlled refusal'));
 expect(await saving).toBe(false);expect(useCollimateStore.getState().cellCache[cell.id].files).toEqual(newer);
});
