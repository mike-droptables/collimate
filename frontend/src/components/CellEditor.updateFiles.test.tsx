// @vitest-environment jsdom
import {act,renderHook} from '@testing-library/react';
import {beforeEach,describe,expect,it,vi} from 'vitest';
import {useCellFileWrites} from './cellEditor/useCellFileWrites';
import {useCollimateStore} from '@/stores/useCollimateStore';
import type {Cell,VirtualFile} from '@/lib/types';
const h=vi.hoisted(()=>({replace:vi.fn(async(_channel:string,_cell:string,_files:VirtualFile[],_baseline:VirtualFile[])=>{})}));
vi.mock('@/lib/fileCommands',async original=>({...await original<typeof import('@/lib/fileCommands')>(),replaceCellFiles:h.replace}));
const file=(name:string,revision=1):VirtualFile=>({name,language:'text',file_revision:revision,body_hash:`hash-${name}`,encoding:'utf-8',writer_id:'writer',edit_seq:1});
let current:Cell,meta:ReturnType<typeof vi.fn<(patch:Partial<Cell>)=>Promise<void>>>;
beforeEach(()=>{vi.clearAllMocks();current={id:'cell',name:'Cell',files:[file('a.txt'),file('_runs/finished/ui.txt')]};meta=vi.fn(async()=>{});useCollimateStore.setState({cellCache:{cell:current},activeChannel:{id:'channel',name:'channel',nav_stacks:[],main_stacks:[],nav_selection_id:null,main_selection_id:null}});});
const hook=()=>renderHook(()=>useCellFileWrites({cell:current,channelId:'channel',readOnly:false,lockOwnerOf:()=>null,save:{latestCell:()=>current,commitCell:meta,notifyEdit:vi.fn()},onLayoutChange:vi.fn()}));
describe('CellEditor structural file writer',()=>{
 it('evaluates updater against the latest files and preserves inactive run-owned refs',async()=>{
  const {result}=hook();current={...current,files:[...current.files,file('later.txt',9)]};
  await act(async()=>{await result.current.handleUpdateFiles(files=>files.filter(f=>f.name==='later.txt'));});
  expect(h.replace).toHaveBeenCalledWith('channel','cell',[file('later.txt',9),file('_runs/finished/ui.txt')],current.files,undefined);
 });
 it('refuses structural creation under run-owned paths',async()=>{
  const {result}=hook();await act(async()=>{await result.current.handleUpdateFiles(files=>[...files,file('_runs/new/ui.txt')]);});
  expect(h.replace.mock.calls[0][2].map((f:VirtualFile)=>f.name)).not.toContain('_runs/new/ui.txt');
 });
 it('propagates refused writes and does not publish accepted pin repair before persistence',async()=>{
  current={...current,pinned:'a.txt'};h.replace.mockRejectedValueOnce(new Error('refused'));const {result}=hook();
  await act(async()=>{await expect(result.current.handleUpdateFiles(files=>files.filter(f=>f.name!=='a.txt'))).rejects.toThrow('refused');});expect(meta).not.toHaveBeenCalled();
 });
 it('hands a delayed copy the current writer and withdraws it on unmount',async()=>{
  const {result,unmount}=hook();const target=result.current.fileImportContext.current!;
  expect(target).toMatchObject({cellId:'cell',channelId:'channel',canWrite:true});
  await act(async()=>{await target.updateFiles(files=>[...files,file('copy.txt')]);});
  expect(h.replace.mock.calls[0][2].map((f:VirtualFile)=>f.name)).toContain('copy.txt');
  unmount();expect(result.current.fileImportContext.current).toBeNull();
 });
});
