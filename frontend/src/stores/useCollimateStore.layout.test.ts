// @vitest-environment jsdom
import { fileFixture } from '@/test/fileFixtures';
import {expect,it} from 'vitest';
import {useCollimateStore} from './useCollimateStore';
import type {Channel} from '@/lib/types';
it('layout-only responses reuse cached file bodies after a cell moves __F0305',()=>{
 const source:Channel={id:'layout-channel',name:'layout',nav_stacks:[],nav_selection_id:null,main_selection_id:'left',main_stacks:[{id:'left',name:'',cells:[{id:'layout-cell',name:'before',updated_at:'2026-01-01T00:00:00Z',files:[fileFixture('large.txt', 'kept body', {language:'text', timestamp:''})]}]}]};
 useCollimateStore.setState({state:null,cellCache:{}});useCollimateStore.getState().applyServerState({active_channel_id:source.id,channels:[source]});
 const files=useCollimateStore.getState().cellCache['layout-cell'].files;
 const layout={...source,main_selection_id:'right',main_stacks:[{id:'right',name:'',cells:[{id:'layout-cell',name:'moved',updated_at:'2026-01-02T00:00:00Z'}]}]};
 useCollimateStore.getState().applyOptimisticLayout(layout);
 expect(useCollimateStore.getState().activeChannel!.main_stacks[0].id).toBe('right');expect(useCollimateStore.getState().cellCache['layout-cell'].files).toBe(files);expect(useCollimateStore.getState().cellCache['layout-cell'].name).toBe('moved');
});
