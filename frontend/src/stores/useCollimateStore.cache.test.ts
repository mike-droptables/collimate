// @vitest-environment jsdom
import {afterEach,expect,it,vi} from 'vitest';
import {useCollimateStore} from './useCollimateStore';
const snapshot={active_channel_id:'cache-channel',channels:[{id:'cache-channel',name:'cache',nav_stacks:[],nav_selection_id:null,main_selection_id:'cache-stack',main_stacks:[{id:'cache-stack',name:'',cells:[{id:'cache-cell',name:'',files:[]}]}]}]};
afterEach(()=>vi.useRealTimers());
it('removed cell bodies survive a grace window then leave the normalized cache __F0531',()=>{
 vi.useFakeTimers();useCollimateStore.setState({state:null,cellCache:{},runs:{}});useCollimateStore.getState().applyServerState(snapshot);
 useCollimateStore.getState().applyServerState({active_channel_id:null,channels:[]});expect(useCollimateStore.getState().cellCache['cache-cell']).toBeDefined();
 vi.advanceTimersByTime(60_001);expect(useCollimateStore.getState().cellCache['cache-cell']).toBeUndefined();
});
it('a cell restored during the grace window retains its accepted metadata cache __F0531',()=>{
 vi.useFakeTimers();useCollimateStore.setState({state:null,cellCache:{},runs:{}});useCollimateStore.getState().applyServerState(snapshot);const cell=useCollimateStore.getState().cellCache['cache-cell'];
 useCollimateStore.getState().applyServerState({active_channel_id:null,channels:[]});vi.advanceTimersByTime(20_000);useCollimateStore.getState().applyServerState(snapshot);vi.advanceTimersByTime(60_001);expect(useCollimateStore.getState().cellCache['cache-cell']).toEqual(cell);
});
