// @vitest-environment jsdom
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {act,renderHook} from '@testing-library/react';
import {useChannelSSE} from './useChannelSSE';
import {useCollimateStore} from '@/stores/useCollimateStore';
import * as api from '@/lib/api';
vi.mock('@/contexts/GuestPoolContext',()=>({useGuestPool:()=>({broadcastToCell})}));
vi.mock('@/lib/fileCommands',()=>({applyMutationResult:vi.fn(),recoverFileOperations:vi.fn(async()=>undefined)}));
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
  emit(value:unknown){this.onmessage?.({data:JSON.stringify(value)});}
}
const snapshot=(revision:string,scope='a')=>({type:'state_snapshot',revision,state:{channel_scope:scope,active_channel_id:scope,channels:[{id:scope,name:scope,main_stacks:[],nav_stacks:[],main_selection_id:null,nav_selection_id:null}]}});
beforeEach(()=>{
  Stream.instances=[];vi.stubGlobal('EventSource',Stream);
  useCollimateStore.setState({state:null,activeChannel:null,cellCache:{},_stateStreamSnapshot:null,_stateStreamRevision:null,_stateGeneration:0,hasInitialized:false});
});
afterEach(()=>{vi.unstubAllGlobals();vi.restoreAllMocks();});
it('P4F03 reconnects on revision gaps and unknown members, ignoring all callbacks from a retired stream',()=>{
  const view=renderHook(()=>useChannelSSE('a'));
  const first=Stream.instances[0];
  act(()=>{first.onopen?.();first.emit(snapshot('one'));});
  expect(useCollimateStore.getState()._stateStreamRevision).toBe('one');
  act(()=>first.emit({type:'state_delta',base_revision:'missing',revision:'two',channel_scope:'a',patches:[]}));
  expect(first.close).toHaveBeenCalled();expect(Stream.instances).toHaveLength(2);
  act(()=>{first.emit(snapshot('stale'));first.onopen?.();});
  expect(useCollimateStore.getState()._stateStreamRevision).toBeNull();
  const second=Stream.instances[1];
  act(()=>{second.onopen?.();second.emit(snapshot('fresh'));});
  act(()=>second.emit({type:'state_delta',base_revision:'fresh',revision:'bad',channel_scope:'a',patches:[{op:'remove',path:['channels',{key:'id',value:'absent'}]}]}));
  expect(Stream.instances).toHaveLength(3);
  expect(useCollimateStore.getState().state!.channels[0].id).toBe('a');
  view.unmount();
});
it('P4F03 overflow and a channel switch require full baselines and retire old-scope events',()=>{
  const view=renderHook(({channel})=>useChannelSSE(channel),{initialProps:{channel:'a'}});
  const first=Stream.instances[0];
  act(()=>{first.onopen?.();first.emit(snapshot('a1'));first.emit({type:'overflow'});});
  const second=Stream.instances[1];
  act(()=>{second.onopen?.();second.emit(snapshot('a2'));});
  view.rerender({channel:'b'});
  const third=Stream.instances[2];
  act(()=>{third.onopen?.();third.emit(snapshot('b1','b'));second.emit(snapshot('late-a'));});
  expect(useCollimateStore.getState().activeChannel!.id).toBe('b');
  expect(useCollimateStore.getState()._stateStreamRevision).toBe('b1');
  view.unmount();
  act(()=>third.emit(snapshot('after-unmount')));
  expect(useCollimateStore.getState()._stateStreamRevision).toBe('b1');
});


it('P4F03 refuses a wrong-scope current-stream delta atomically and requests a fresh source',()=>{
  const view=renderHook(()=>useChannelSSE('a'));
  const first=Stream.instances[0];
  act(()=>{first.onopen?.();first.emit(snapshot('one'));});
  const before=useCollimateStore.getState();
  const event={type:'state_delta' as const,base_revision:'one',revision:'wrong-scope',channel_scope:'b',
    patches:[{op:'set' as const,path:['channels',{key:'id' as const,value:'a'},'name'],value:'partial mutation'}]};
  expect(before.applyStateEvent(event)).toBe(false);
  expect(useCollimateStore.getState()).toBe(before);
  act(()=>first.emit(event));
  expect(first.close).toHaveBeenCalled();
  expect(Stream.instances).toHaveLength(2);
  expect(useCollimateStore.getState().state).toBe(before.state);
  expect(useCollimateStore.getState().activeChannel!.name).toBe('a');
  expect(useCollimateStore.getState()._stateStreamSnapshot).toBeNull();
  expect(useCollimateStore.getState()._stateStreamRevision).toBeNull();
  view.unmount();
});

it.each(['publication','replacement'])('P4F03 ignores held active-channel hydration after a newer %s',async mode=>{
  let complete!: (state: Awaited<ReturnType<typeof api.fetchState>>) => void;
  const fetch=vi.spyOn(api,'fetchState').mockImplementation(()=>new Promise(resolve=>{complete=resolve;}));
  const view=renderHook(({channel})=>useChannelSSE(channel),{initialProps:{channel:'a'}});
  const first=Stream.instances[0];
  const announced=snapshot('one');
  announced.state.active_channel_id='b';
  announced.state.channels.push({...snapshot('metadata','b').state.channels[0],name:'old B metadata'});
  act(()=>{first.onopen?.();first.emit(announced);});
  expect(fetch).toHaveBeenCalledOnce();
  let current=first;
  if(mode==='replacement'){
    view.rerender({channel:'b'});
    current=Stream.instances[1];
    expect(first.close).toHaveBeenCalledOnce();
  }
  const fresh=snapshot('fresh','b');
  fresh.state.channels[0].name='fresh B';
  act(()=>{if(mode==='replacement')current.onopen?.();current.emit(fresh);});
  const accepted=useCollimateStore.getState();
  await act(async()=>{complete(snapshot('obsolete','a').state);await Promise.resolve();});
  expect(useCollimateStore.getState()).toBe(accepted);
  expect(useCollimateStore.getState().activeChannel!.name).toBe('fresh B');
  expect(useCollimateStore.getState()._stateStreamRevision).toBe('fresh');
  view.unmount();
});
