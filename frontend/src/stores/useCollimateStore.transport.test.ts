// @vitest-environment jsdom
import { fileFixture } from '@/test/fileFixtures';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useCollimateStore } from './useCollimateStore';
import { applyStatePatches } from './collimate/reconcile';
import type { ScopedAppState, StatePatch, StatePathPart } from '@/lib/types';
import * as api from '@/lib/api';
import { fixtureBodyHash } from '@/test/fileFixtures';

const cellPath: StatePathPart[] = ['channels', {key:'id',value:'a'}, 'main_stacks', {key:'id',value:'s'}, 'cells', {key:'id',value:'c'}];
const filePath = (name = 'data.txt'): StatePathPart[] => [...cellPath, 'files', {key:'name',value:name}];
const baseline = (): ScopedAppState => ({active_channel_id:'a', channel_scope:'a', channels:[{
  id:'a', name:'A', main_selection_id:'s', nav_selection_id:null, nav_stacks:[],
  main_stacks:[{id:'s',name:'S',cells:[{id:'c',name:'C',updated_at:'2026-01-01T00:00:00Z',files:[
    fileFixture('data.txt', 'old', {language:'text'}),
    fileFixture('untouched.txt', 'retained', {language:'text'}),
  ]}]}],
}]});
const start = () => expect(useCollimateStore.getState().applyStateEvent({type:'state_snapshot',revision:'epoch:1',state:baseline()})).toBe(true);
const delta = (patches: StatePatch[], revision='epoch:2', base_revision='epoch:1') =>
  useCollimateStore.getState().applyStateEvent({type:'state_delta',revision,base_revision,channel_scope:'a',patches});
beforeEach(() => useCollimateStore.setState({state:null,activeChannel:null,cellCache:{},_stateStreamSnapshot:null,_stateStreamRevision:null,_stateGeneration:0,apiError:null}));
afterEach(() => vi.restoreAllMocks());

it('P4F03 applies keyed deltas atomically, retaining untouched files and immutable prior wire snapshots', () => {
  start();
  const prior = useCollimateStore.getState()._stateStreamSnapshot!;
  const untouched = useCollimateStore.getState().cellCache.c.files[1];
  expect(delta([{op:'set',path:filePath(),value:fileFixture('data.txt','next',{language:'text',file_revision:2})}, {op:'set',path:[...cellPath,'updated_at'],value:'2026-01-01T00:00:01Z'}])).toBe(true);
  expect(useCollimateStore.getState().cellCache.c.files[0].body_hash).toBe(fixtureBodyHash('next'));
  expect(useCollimateStore.getState().cellCache.c.files[1]).toBe(untouched);
  expect(JSON.stringify(prior)).toContain(fixtureBodyHash('old'));
  const accepted = useCollimateStore.getState();
  expect(delta([{op:'set',path:[...filePath(),'body_hash'],value:'partial'}, {op:'remove',path:filePath('missing')}], 'epoch:3','epoch:2')).toBe(false);
  expect(useCollimateStore.getState()).toBe(accepted);
  expect(delta([], 'epoch:4','epoch:missing')).toBe(false);
  expect(delta([], 'epoch:2','epoch:1')).toBe(true); // exact replay is a no-op
  expect(useCollimateStore.getState()).toBe(accepted);
});

it('P4F03 treats literal special file names safely, refuses unknown add/remove, and preserves ordered membership', () => {
  const original = baseline();
  for (const name of ['__proto__', 'constructor', '目录/a~b.txt']) {
    const value = fileFixture(name, 'special', {language:'text'});
    const inserted = applyStatePatches(original,[{op:'insert',path:filePath(name),index:0,value}]);
    const moved = applyStatePatches(inserted,[{op:'move',path:filePath(name),index:2}]);
    expect(JSON.stringify(moved)).toContain(value.body_hash);
    expect(applyStatePatches(moved,[{op:'remove',path:filePath(name)}])).toEqual(original);
    expect(() => applyStatePatches(inserted,[{op:'insert',path:filePath(name),index:0,value}])).toThrow();
    expect(() => applyStatePatches(original,[{op:'remove',path:filePath(name)}])).toThrow();
  }
  const named = applyStatePatches(original,[{op:'set',path:['__proto__'],value:{polluted:true}}]);
  expect(Object.hasOwn(named,'__proto__')).toBe(true);
  expect(Object.getPrototypeOf(named)).toBe(Object.prototype);
  expect(Object.hasOwn({},'polluted')).toBe(false);
  expect(() => applyStatePatches(original,[{op:'set',path:['__proto__','polluted'],value:true}])).toThrow();
});

it.each([false,true])('P4F03 ignores a stale HTTP completion after a stream publication (failure=%s)', async failure => {
  let answer!: (value: ScopedAppState) => void;
  let reject!: (error: Error) => void;
  vi.spyOn(api,'fetchState').mockImplementation(() => new Promise((resolve, fail) => {answer=resolve;reject=fail;}));
  const fetch = useCollimateStore.getState().fetchState();
  start();
  if (failure) reject(new Error('obsolete error')); else answer({...baseline(),active_channel_id:null});
  await fetch;
  expect(useCollimateStore.getState().state!.active_channel_id).toBe('a');
  expect(useCollimateStore.getState()._stateStreamRevision).toBe('epoch:1');
  expect(useCollimateStore.getState().apiError).toBeNull();
});

it('P4F03 accepts a long sequence without accumulating history and requires a new baseline after reconnect', () => {
  start();
  for (let n=2;n<=501;n++) expect(delta([{op:'set',path:['channels',{key:'id',value:'a'},'name'],value:`A${n}`}],`epoch:${n}`,`epoch:${n-1}`)).toBe(true);
  expect(useCollimateStore.getState().activeChannel!.name).toBe('A501');
  expect(JSON.stringify(useCollimateStore.getState()._stateStreamSnapshot).length).toBeLessThan(1024);
  useCollimateStore.getState().resetStateStream();
  expect(delta([], 'epoch:502','epoch:501')).toBe(false);
  start();
  expect(useCollimateStore.getState().activeChannel!.name).toBe('A');
});

it('P4F03 compact file ordering matches Python code-point order and leaves intentional moves available', () => {
  const names=['𐀀.txt','\uE000.txt','__proto__','constructor','目录/a~b.txt','2','10'];
  let state=baseline();
  state=applyStatePatches(state,[{op:'set',path:[...cellPath,'files'],value:names.map(name=>(fileFixture(name, name, {language:'text'})))}]);
  const original=state;
  const ordered=applyStatePatches(state,[{op:'sort',path:[...cellPath,'files']}]);
  const files=(ordered.channels[0] as import('@/lib/types').Channel).main_stacks[0].cells[0].files;
  expect(files.map(file=>file.name)).toEqual(['10','2','__proto__','constructor','目录/a~b.txt','\uE000.txt','𐀀.txt']);
  expect((original.channels[0] as import('@/lib/types').Channel).main_stacks[0].cells[0].files.map(file=>file.name)).toEqual(names);
  const moved=applyStatePatches(ordered,[{op:'move',path:filePath('𐀀.txt'),index:0}]);
  expect((moved.channels[0] as import('@/lib/types').Channel).main_stacks[0].cells[0].files[0].name).toBe('𐀀.txt');
});

it('P4F03 canonical SDK snapshot and delta reach the actual store with matching keyed paths', async () => {
  const {default:samples}=await import('../../../sdk/shared/contracts/channel-events.json');
  const events=samples.filter(event=>event.type==='state_snapshot'||event.type==='state_delta') as import('./collimate/stateSlice').StateEvent[];
  for(const event of events) expect(useCollimateStore.getState().applyStateEvent(event)).toBe(true);
  expect(useCollimateStore.getState().cellCache['cell-1'].files[0].body_hash).toBe(fixtureBodyHash('after'));
  expect(useCollimateStore.getState()._stateStreamRevision).toBe('fixture:2');
});

it('metadata revisions distinguish empty text and binary without eager body alternatives', () => {
  start();
  const empty=fileFixture('data.txt','',{file_revision:2});
  expect(delta([{op:'set',path:filePath(),value:empty}])).toBe(true);
  expect(useCollimateStore.getState().cellCache.c.files[0]).toEqual(empty);
  const binary={...empty,encoding:'binary' as const,file_revision:3,body_hash:'b'.repeat(64)};
  expect(delta([{op:'set',path:filePath(),value:binary}],'epoch:3','epoch:2')).toBe(true);
  expect(useCollimateStore.getState().cellCache.c.files[0]).toEqual(binary);
  expect(useCollimateStore.getState().cellCache.c.files[0]).not.toHaveProperty('content');
});

it.each(['insert','move'] as const)('P4F03 refuses an invalid %s index without publishing an earlier valid patch', operation => {
  start();
  const before=useCollimateStore.getState();
  const invalid: StatePatch=operation==='move'
    ? {op:'move',path:filePath(),index:2}
    : {op:'insert',path:filePath('new.txt'),index:-1,value:fileFixture('new.txt', 'new', {language:'text'})};
  expect(delta([{op:'set',path:[...filePath(),'body_hash'],value:'partial mutation'},invalid])).toBe(false);
  expect(useCollimateStore.getState()).toBe(before);
  expect(useCollimateStore.getState()._stateStreamSnapshot).toBe(before._stateStreamSnapshot);
  expect(useCollimateStore.getState()._stateStreamRevision).toBe('epoch:1');
  expect(useCollimateStore.getState().cellCache.c.files[0].body_hash).toBe(fixtureBodyHash('old'));
});
