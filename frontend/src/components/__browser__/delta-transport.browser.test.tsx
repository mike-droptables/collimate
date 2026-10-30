import { fixtureText } from '@/test/fileFixtures';
import { fileFixture } from '@/test/fileFixtures';
import {expect,it,vi} from 'vitest';
import {render} from 'vitest-browser-react';
import {page} from 'vitest/browser';
import {useChannelSSE} from '@/hooks/useChannelSSE';
import {useCollimateStore} from '@/stores/useCollimateStore';
import type {StatePathPart} from '@/lib/types';

vi.mock('@/contexts/GuestPoolContext',()=>({useGuestPool:()=>({broadcastToCell})}));
vi.mock('@/lib/runAttach',()=>({recoverActiveRuns:vi.fn(async()=>{}),attachStartedRun:vi.fn(),dispatchRunEvents:vi.fn(),dispatchRunPhase:vi.fn(),dispatchRunStatus:vi.fn()}));
const broadcastToCell=vi.fn();
class Stream {
  static instances:Stream[]=[];
  onopen:(()=>void)|null=null;
  onmessage:((event:{data:string})=>void)|null=null;
  onerror:(()=>void)|null=null;
  readyState=1;
  close=vi.fn();
  constructor(public url:string){Stream.instances.push(this);}
  emit(event:unknown){this.onmessage?.({data:JSON.stringify(event)});}
}
const cellPath:StatePathPart[]=['channels',{key:'id',value:'a'},'main_stacks',{key:'id',value:'s'},'cells',{key:'id',value:'c'}];
function Viewer(){
  useChannelSSE('a');
  const cell=useCollimateStore(state=>state.cellCache.c);
  const file=cell?.files.find(file=>file.name==='f1.txt');
  return <div><output aria-label="Current file">{file?fixtureText(file).slice(0,16):'waiting'}</output>
    <output aria-label="File count">{cell?.files.length ?? 0}</output></div>;
}
it.each([20,200,2000])('P4F03 browser parses, applies and renders a bounded delta after a %i-file baseline',async count=>{
  Stream.instances=[];vi.stubGlobal('EventSource',Stream);
  useCollimateStore.setState({state:null,activeChannel:null,cellCache:{},_stateStreamSnapshot:null,_stateStreamRevision:null,_stateGeneration:0,hasInitialized:false});
  // Inventory stays metadata-only at every size; immutable bytes live separately.
  const files=Array.from({length:count},(_,i)=>(fileFixture(`f${i}.txt`, i%20===0?'':'x'.repeat(48*1024), {language:'text', timestamp:''})));
  const state={active_channel_id:'a',channel_scope:'a',channels:[{id:'a',name:'A',main_selection_id:'s',nav_selection_id:null,nav_stacks:[],main_stacks:[{id:'s',name:'S',cells:[{id:'c',name:'C',updated_at:'2026-01-01T00:00:00Z',files}]}]}]};
  const view=await render(<Viewer/>);
  try {
    const first=Stream.instances[0];first.onopen?.();first.emit({type:'state_snapshot',revision:'one',state});
    await expect.element(page.getByLabelText('Current file')).toMatchTextContent('xxxxxxxx');
    const untouched=useCollimateStore.getState().cellCache.c.files[2];
    const delta={type:'state_delta',base_revision:'one',revision:'two',channel_scope:'a',patches:[
      {op:'sort',path:[...cellPath,'files']},
      {op:'set',path:[...cellPath,'files',{key:'name',value:'f1.txt'}],value:fileFixture('f1.txt','changed '+ 'a'.repeat(1016),{language:'text',file_revision:2})},
      {op:'set',path:[...cellPath,'updated_at'],value:'2026-01-01T00:00:01Z'},
    ]};
    const wire=JSON.stringify(delta);expect(new TextEncoder().encode(wire).length).toBeLessThan(4096);
    const started=performance.now();first.onmessage?.({data:wire});const parseApplyMs=performance.now()-started;
    await expect.element(page.getByLabelText('Current file')).toMatchTextContent('changed');
    await expect.element(page.getByLabelText('File count')).toMatchTextContent(String(count));
    expect(useCollimateStore.getState().cellCache.c.files.find(file=>file.name==='f2.txt')).toBe(untouched);
    console.log('P4F03_BROWSER',JSON.stringify({files:count,deltaBytes:new TextEncoder().encode(wire).length,parseApplyMs}));
    first.emit({type:'state_delta',base_revision:'gap',revision:'bad',channel_scope:'a',patches:[]});
    await vi.waitFor(()=>expect(Stream.instances).toHaveLength(2));
    const recovered=structuredClone(state);recovered.channels[0].main_stacks[0].cells[0].updated_at='2026-01-01T00:00:02Z';
    recovered.channels[0].main_stacks[0].cells[0].files[1]=fileFixture('f1.txt','x'.repeat(48*1024),{language:'text',file_revision:3});
    const next=Stream.instances[1];next.onopen?.();next.emit({type:'state_snapshot',revision:'fresh',state:recovered});
    await expect.element(page.getByLabelText('Current file')).toMatchTextContent('xxxxxxxx');
    first.emit(delta);
    expect(useCollimateStore.getState()._stateStreamRevision).toBe('fresh');
  } finally {await view.unmount();vi.unstubAllGlobals();}
});
