// @vitest-environment jsdom
import { fileFixture } from '@/test/fileFixtures';
import {beforeEach,expect,it} from 'vitest';
import {useCollimateStore} from './useCollimateStore';
import type {AppState,Channel} from '@/lib/types';
const channel=(id:string):Channel=>({id,name:id,nav_stacks:[],nav_selection_id:null,main_selection_id:'stack-'+id,main_stacks:[{id:'stack-'+id,name:'',cells:[{id:'cell-'+id,name:'',updated_at:'2026-01-01T00:00:00Z',files:[fileFixture('data.txt', 'body-'+id, {language:'text'})]}]}]});
beforeEach(()=>useCollimateStore.setState({state:null,cellCache:{},activeChannel:null}));
it('scoped state preserves other channel content and removes absent channel IDs __F0967',()=>{
 const s=useCollimateStore.getState();s.applyServerState({active_channel_id:'a',channels:[channel('a'),channel('b'),channel('removed')]});
 const before=useCollimateStore.getState();const b=before.state!.channels[1];
 s.applyServerState({active_channel_id:'a',channel_scope:'a',channels:[channel('a'),{id:'b',name:'renamed'}]} as unknown as AppState);
 const next=useCollimateStore.getState();expect(next.state!.channels.map(c=>c.id)).toEqual(['a','b']);expect(next.state!.channels[1].name).toBe('renamed');expect(next.state!.channels[1].main_stacks).toBe(b.main_stacks);expect(next.cellCache['cell-b'].files[0].body_hash).toBe(channel('b').main_stacks[0].cells[0].files[0].body_hash);
});
it('metadata-only new channel never erases already hydrated active cell data __F0967',()=>{
 const s=useCollimateStore.getState();s.applyServerState({active_channel_id:'a',channels:[channel('a')]});
 s.applyServerState({active_channel_id:'a',channel_scope:'a',channels:[channel('a'),{id:'new',name:'new'}]} as unknown as AppState);
 expect(useCollimateStore.getState().activeChannel!.main_stacks[0].cells[0].files[0].body_hash).toBe(channel('a').main_stacks[0].cells[0].files[0].body_hash);expect(useCollimateStore.getState().state!.channels[1].main_stacks).toEqual([]);
});
