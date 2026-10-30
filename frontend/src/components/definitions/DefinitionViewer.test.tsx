import { fixtureText, fixtureBodyHash } from '@/test/fileFixtures';
import { fileFixture } from '@/test/fileFixtures';
import { routeApiToFetchStub } from '@/test/apiTransport';
import {beforeEach, afterEach, describe, expect, it, vi} from 'vitest';
import {act, fireEvent, render, screen, waitFor} from '@testing-library/react';
import DefinitionViewer from './DefinitionViewer';
import {DefinitionForm} from './DefinitionForm';
import type {VirtualFile} from '@/lib/types';
import {parseDefinition} from '@/lib/definitionFiles';
import {fetchHandlers} from '@/lib/definitionApi';
vi.mock('@/lib/runnerApi', async original => ({
 ...(await original<Record<string,unknown>>()),
 fetchRunnerGenerators:vi.fn(async()=>[]),
 fetchRunnerVocabulary:vi.fn(async()=>({enums:{docker:['none','limited','full'],internet:['none','proxy','full'],commands:['deny','ask','allow']}, defaults:{docker:'none',internet:'full',commands:'deny',update_packages:true,internet_allow:[]},labels:{}, descriptions: {}})),
 fetchRunnerConfig:vi.fn(()=>{throw new Error('Draft config must come from the cell');}),
 fetchRunners:vi.fn(async()=>[{id:'job',name:'Job runner',generator:'writer'},{id:'other',name:'Other runner',generator:'elsewhere'}]),
}));
vi.mock('@/lib/definitionApi', async original => ({
 ...(await original<Record<string,unknown>>()),
 fetchHandlers:vi.fn(async()=>[{id:'notes',builtin:true},{id:'check',builtin:false}]),
}));
vi.mock('@/lib/accountsApi', async original => ({...(await original<Record<string,unknown>>()),fetchAccounts:vi.fn(async()=>({accounts:[],services:[],status:{available:true}}))}));
const vf=(name:string,content:string, extra:Partial<VirtualFile>={}):VirtualFile=>(fileFixture(name, content, {timestamp:'stamp', language:'text', ...extra}));
const runner=vf('draft/job.runner','id: job\nname: Job\ngenerator: writer\nsettings:\n  settings.set_yaml:\n    title: Original\n');
const files=[runner,vf('gen/choice.generator','id: writer\nname: Draft writer\nmain_config: settings.set_yaml\nconfigs:\n  - id: other\n    name: Local config\n    path: configs/other/config.set_yaml\n'),vf('gen/settings.set_yaml','fields:\n  - key: title\n    label: Draft title\n    type: text\n    default: Draft default\n'),vf('gen/configs/other/config.set_yaml','settings:\n  settings.set_yaml:\n    title: Config title\n'),vf('gen/configs/other/files/config.txt','config bytes'),vf('draft/files/body.txt','body'),vf('draft/files/picture.bin','',{language:'binary',body_hash: 'binary-hash', encoding: 'binary'}),vf('unrelated.txt','',{body_hash: 'unloaded-hash', encoding: 'binary'})];
let restoreTransport: () => void;
beforeEach(()=>{vi.clearAllMocks();restoreTransport=routeApiToFetchStub();});
afterEach(()=>restoreTransport());
describe('app-owned definition forms',()=>{
 it('refuses malformed YAML without replacing it with an empty mapping__D13',async()=>{
  const save=vi.fn();render(<DefinitionForm content="name: [" readOnly={false} onSave={save}/>);
  expect(await screen.findByRole('alert')).toBeTruthy();
  expect(screen.getByRole('button',{name:'Save definition'})).toBeDisabled();
  fireEvent.click(screen.getByRole('button',{name:'Save definition'}));expect(save).not.toHaveBeenCalled();
 });
 it('uses the same-cell draft generator schema and writes only runner-owned files__D13',async()=>{
  let current=[...files];const save=vi.fn();
  render(<DefinitionViewer path={runner.name} content={fixtureText(runner)} files={files} cellId="cell" channel="ch" readOnly={false} onSave={save} onUpdateFiles={fn=>{current=fn(current);}}/>);
  expect(await screen.findByLabelText('Draft title')).toHaveValue('Original');
  fireEvent.change(screen.getByLabelText('Draft title'),{target:{value:'Changed'}});
  current=[...current,vf('draft/files/concurrent.txt','created concurrently')];
  fireEvent.click(screen.getByRole('button',{name:'Save'}));
  await waitFor(()=>expect(parseDefinition(fixtureText(current.find(f=>f.name===runner.name)!)).settings).toEqual({'settings.set_yaml':{title:'Changed'}}));
  expect(current.find(f=>f.name==='unrelated.txt')).toBe(files.find(f=>f.name==='unrelated.txt'));
  expect(current.find(f=>f.name==='draft/files/picture.bin')?.body_hash).toBe('binary-hash');
  expect(fixtureText(current.find(f=>f.name==='draft/files/concurrent.txt'))).toBe('created concurrently');
  expect(current.find(f=>f.name==='gen/settings.set_yaml')).toBe(files.find(f=>f.name==='gen/settings.set_yaml'));
  expect(save).not.toHaveBeenCalled();
 });
 it('loads same-cell custom files without changing settings or using the approved catalog__D13',async()=>{
  let current=[...files];
  render(<DefinitionViewer path={runner.name} content={fixtureText(runner)} files={files} cellId="cell" channel="ch" readOnly={false} onSave={vi.fn()} onUpdateFiles={fn=>{current=fn(current);}}/>);
  await screen.findByLabelText('Draft title');fireEvent.click(screen.getByRole('tab',{name:'Files'}));fireEvent.click(screen.getByRole('button',{name:'Load file sets'}));
  fireEvent.click(screen.getByRole('radio',{name:'Local config'}));
  fireEvent.click(screen.getByRole('button',{name:'Confirm'}));
  await screen.findByLabelText('Contents of config.txt');
  fireEvent.click(screen.getByRole('tab',{name:'Config'}));
  expect(screen.getByLabelText('Draft title')).toHaveValue('Original');
  fireEvent.click(screen.getByRole('button',{name:'Save'}));
  await waitFor(()=>expect(fixtureText(current.find(f=>f.name==='draft/files/config.txt'))).toBe('config bytes'));
  expect(parseDefinition(fixtureText(current.find(f=>f.name===runner.name)!)).settings).toEqual({'settings.set_yaml':{title:'Original'}});
  expect(current.find(f=>f.name==='draft/files/body.txt')).toBeUndefined();
  expect(current.find(f=>f.name==='unrelated.txt')).toBe(files.find(f=>f.name==='unrelated.txt'));
 });
 it('refuses an intervening runner-package edit instead of overwriting it__D13',async()=>{
  let current=[...files];
  render(<DefinitionViewer path={runner.name} content={fixtureText(runner)} files={files} cellId="cell" channel="ch" readOnly={false} onSave={vi.fn()} onUpdateFiles={fn=>{current=fn(current);}}/>);
  await screen.findByLabelText('Draft title');current=current.map(f=>f.name==='draft/files/body.txt'?{...f,body_hash:fixtureBodyHash('new external bytes'),file_revision:2}:f);
  fireEvent.click(screen.getByRole('button',{name:'Save'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('runner package changed');
  expect(fixtureText(current.find(f=>f.name==='draft/files/body.txt'))).toBe('new external bytes');
 });
});

it('shows only the selected definition mounted hash and ignores an older mount cleanup__D13_D14',async()=>{
 const props={content:'id: view\nname: View',files:[],cellId:'cell',channel:'ch',readOnly:false,onSave:vi.fn()};
 const view=render(<DefinitionViewer {...props} path="one/view.renderer"/>);
 const report=(mountId:string,contentHash:string,active:boolean)=>act(()=>{window.dispatchEvent(new CustomEvent('collimate:renderer-preview',{detail:{cellId:'cell',definitionPath:'one/view.renderer',mountId,contentHash,active}}));});
 report('old','old-hash',true);report('current','new-hash',true);report('old','old-hash',false);
 expect(screen.getByText('new-hash')).toBeTruthy();expect(screen.queryByText('old-hash')).toBeNull();
 view.rerender(<DefinitionViewer {...props} path="two/view.renderer"/>);
 expect(screen.queryByText('new-hash')).toBeNull();
});

it('keeps malformed config rows editable without crashing__D13',()=>{
 render(<DefinitionViewer path="gen/main.generator" content="configs: [null]" files={[]} cellId="cell" channel="ch" readOnly={false} onSave={vi.fn()}/>);
 expect(screen.getByText(/Invalid named config/)).toBeTruthy();
 expect(screen.getByLabelText('configs')).toHaveValue('[\n  null\n]');
});
it('edits handler example feedback and opens its actual files__D13',async()=>{
 const save=vi.fn(),open=vi.fn();
 render(<DefinitionViewer path="checks/check.handler" content={"id: check\nexamples:\n  - folder: good\n    description: Good\n    expect: valid\n    feedback: Original\n"} files={[vf('checks/examples/good/view.renderer','bytes')]} cellId="cell" channel="ch" readOnly={false} onSave={save} onOpenFile={open}/>);
 fireEvent.change(await screen.findByLabelText('Example feedback 1'),{target:{value:'Revised'}});
 fireEvent.click(screen.getByRole('button',{name:'checks/examples/good/view.renderer'}));expect(open).toHaveBeenCalledWith('checks/examples/good/view.renderer');
 fireEvent.click(screen.getByRole('button',{name:'Save definition'}));await waitFor(()=>expect(save).toHaveBeenCalled());
 expect((parseDefinition(save.mock.calls[0][0]).examples as {feedback:string}[])[0].feedback).toBe('Revised');
});

it('displays the SDK mapped destination/source files for the selected immutable options definition__D13',async()=>{
 const fetch=vi.spyOn(globalThis,'fetch').mockResolvedValue({ok:true,json:async()=>({options:[{name:'Chosen',files:{'main.tsx':'literal?/source.tsx'}}],kept:['report.txt'],retain_source:true})} as Response);
 try {
 render(<DefinitionViewer path="choices.options_stack" content="options: []" files={[]} cellId="cell" channel="ch" versionId="past" readOnly onSave={vi.fn()}/>);
 expect(await screen.findByText('main.tsx ← literal?/source.tsx')).toBeTruthy();expect(screen.getByText('report.txt')).toBeTruthy();
 expect(String(fetch.mock.calls[0][0])).toContain('version_id=past');
 } finally {fetch.mockRestore();}
});

it('keeps handler example files readable when feedback is immutable__D13',async()=>{
 const open=vi.fn();render(<DefinitionViewer path="checks/check.handler" content={'examples: [{folder: good, feedback: Original}]'} files={[vf('checks/examples/good/view.renderer','bytes')]} cellId="cell" channel="ch" readOnly onSave={vi.fn()} onOpenFile={open}/>);
 expect(await screen.findByLabelText('Example feedback 1')).toBeDisabled();fireEvent.click(screen.getByRole('button',{name:'checks/examples/good/view.renderer'}));expect(open).toHaveBeenCalledWith('checks/examples/good/view.renderer');
});
it('a built-in card\'s handler opens read-only with the built-in note and every example file, the app\'s and the added',async()=>{
 const {useCollimateStore}=await import('@/stores/useCollimateStore');
 const card={id:'defs',name:'Definitions',builtin:true,files:[]};
 const channel={id:'ch',name:'ch',nav_stacks:[],nav_selection_id:null,main_selection_id:null,main_stacks:[{id:'caps',name:'Capabilities',stack_type:'capabilities' as const,cells:[card]}]};
 useCollimateStore.setState({state:{active_channel_id:'ch',channels:[channel]},activeChannel:channel,cellCache:{}} as never);
 try{
  render(<DefinitionViewer path="renderer/renderer.handler" content={'id: renderer\nexamples: [{folder: good, description: Good, expect: valid, feedback: Fine}]\n'} files={[vf('renderer/examples/good/view.renderer','a'),vf('renderer/examples/example-1234abcd/mine.renderer','b')]} cellId="defs" channel="ch" readOnly onSave={vi.fn()}/>);
  expect(await screen.findByLabelText('Example feedback 1')).toBeDisabled();
  expect(screen.getByText('Built-in: its validation is part of the app. Add examples and regenerate skills from its card in Capabilities.')).toBeTruthy();
  expect(screen.getByRole('button',{name:'renderer/examples/good/view.renderer'})).toBeTruthy();
  expect(screen.getByRole('button',{name:'renderer/examples/example-1234abcd/mine.renderer'})).toBeTruthy();
  expect(screen.queryByText(/Validate, build and accept/)).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'Show in Capabilities'}));
  expect(useCollimateStore.getState().capabilitiesManageRequest).toMatchObject({kind:'handler',handler:{cellId:'defs',folder:'renderer'}});
 }finally{useCollimateStore.setState({state:null,activeChannel:null,capabilitiesManageRequest:null} as never);}
});
describe('a definition in use is one on Capabilities',()=>{
 const seat=async(type:'capabilities'|'standard')=>{
  const {useCollimateStore}=await import('@/stores/useCollimateStore');
  const cell={id:'cell',name:'Checks',files:[]};
  const channel={id:'ch',name:'ch',nav_stacks:[],nav_selection_id:null,main_selection_id:null,main_stacks:[{id:'s',name:type==='capabilities'?'Capabilities':'Work',stack_type:type,cells:[cell]}]};
  useCollimateStore.setState({state:{active_channel_id:'ch',channels:[channel]},activeChannel:channel,cellCache:{},capabilitiesManageRequest:null} as never);
  return useCollimateStore;
 };
 afterEach(async()=>{const {useCollimateStore}=await import('@/stores/useCollimateStore');useCollimateStore.setState({state:null,activeChannel:null,capabilitiesManageRequest:null} as never);});
 it('a user handler on Capabilities points at its card there, and Show in Capabilities reveals it',async()=>{
  const store=await seat('capabilities');
  render(<DefinitionViewer path="checks/check.handler" content={'id: check\n'} files={[]} cellId="cell" channel="ch" readOnly={false} onSave={vi.fn()}/>);
  expect(await screen.findByText('Validate, build and accept this handler from its card in Capabilities.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'Show in Capabilities'}));
  expect(store.getState().capabilitiesManageRequest).toMatchObject({kind:'handler',handler:{cellId:'cell',folder:'checks'}});
 });
 it('a handler off Capabilities says how it gets there, with nothing to show',async()=>{
  await seat('standard');
  render(<DefinitionViewer path="checks/check.handler" content={'id: check\n'} files={[]} cellId="cell" channel="ch" readOnly={false} onSave={vi.fn()}/>);
  expect(await screen.findByText('Add it to Capabilities from this cell’s Files panel to validate, build and accept it there.')).toBeTruthy();
  expect(screen.queryByRole('button',{name:'Show in Capabilities'})).toBeNull();
 });
 it('a handler off Capabilities with a built-in handler\'s id (a Regenerate work cell) points at Use these skills, never Add',async()=>{
  await seat('standard');
  render(<DefinitionViewer path="notes/notes.handler" content={'id: notes\n'} files={[]} cellId="cell" channel="ch" readOnly={false} onSave={vi.fn()}/>);
  expect(await screen.findByText('Use these skills from this cell’s Files panel; they then wait for review on the built-in card in Capabilities.')).toBeTruthy();
  expect(screen.queryByText(/Add it to Capabilities/)).toBeNull();
 });
 it('claims neither while the handler catalog loads, nor when it fails',async()=>{
  await seat('standard');
  vi.mocked(fetchHandlers).mockReturnValueOnce(new Promise(()=>{}));
  const view=render(<DefinitionViewer path="notes/notes.handler" content={'id: notes\n'} files={[]} cellId="cell" channel="ch" readOnly={false} onSave={vi.fn()}/>);
  await screen.findByText('Example files');
  expect(screen.queryByText(/Add it to Capabilities|Use these skills/)).toBeNull();
  view.unmount();
  vi.mocked(fetchHandlers).mockRejectedValueOnce(new Error('down'));
  render(<DefinitionViewer path="notes/notes.handler" content={'id: notes\n'} files={[]} cellId="cell" channel="ch" readOnly={false} onSave={vi.fn()}/>);
  await waitFor(()=>expect(fetchHandlers).toHaveBeenCalledTimes(2));
  await act(async()=>{await Promise.resolve();});
  expect(screen.queryByText(/Add it to Capabilities|Use these skills/)).toBeNull();
 });
 it('lists the runners targeting a generator only while it is on Capabilities',async()=>{
  await seat('capabilities');
  const view=render(<DefinitionViewer path="gen/choice.generator" content={'id: writer\nname: Draft writer\n'} files={[]} cellId="cell" channel="ch" readOnly={false} onSave={vi.fn()}/>);
  expect(await screen.findByText('Job runner')).toBeTruthy();
  expect(screen.queryByText('Other runner')).toBeNull();
  view.unmount();
  await seat('standard');
  render(<DefinitionViewer path="gen/choice.generator" content={'id: writer\nname: Draft writer\n'} files={[]} cellId="cell" channel="ch" readOnly={false} onSave={vi.fn()}/>);
  await screen.findByRole('heading',{name:'Named configs'});
  expect(screen.queryByRole('heading',{name:'Runners targeting this generator'})).toBeNull();
 });
});
it('shows invalid SDK options mapping refusal rather than guessed files__D13',async()=>{
 const fetch=vi.spyOn(globalThis,'fetch').mockResolvedValue({ok:false,status:409,json:async()=>({detail:'Invalid options definition'})} as Response);
 try {render(<DefinitionViewer path="choices.options_stack" content="options: []" files={[]} cellId="cell" channel="ch" readOnly onSave={vi.fn()}/>);expect(await screen.findByRole('alert')).toHaveTextContent('Invalid options definition');expect(screen.queryByText('Source retained')).toBeNull();}finally{fetch.mockRestore();}
});

describe('definition forms on the form vocabulary (P04)',()=>{
 const handlerText="id: notes\nname: Notes\nenabled: true\nextensions: [.md]\ntypes:\n  - ext: .note\n  - ext: .memo\nexamples:\n  - folder: good\n    description: Good\n    expect: valid\n    feedback: Fine\n";
 /** Every element carries a class: nothing renders as a bare browser default. */
 const bare=(root:HTMLElement)=>[...root.querySelectorAll('*')].filter(el=>!['OPTION','svg','path','line','polyline','circle','rect'].includes(el.tagName)&&el.closest('svg')===null&&!el.getAttribute('class'));
 it('renders every field, chip and example card with a class: no bare element (P04)',async()=>{
  const {container}=render(<DefinitionForm kind="handler" content={handlerText} readOnly={false} onSave={vi.fn()}/>);
  await screen.findByLabelText('Example feedback 1');
  expect(bare(container).map(el=>el.outerHTML.slice(0,80))).toEqual([]);
 });
 it('heads the examples with a section label, shows types and extensions as chips and a boolean as a switch (P04)',async()=>{
  render(<DefinitionForm kind="handler" content={handlerText} readOnly={false} onSave={vi.fn()}/>);
  expect((await screen.findByRole('heading',{name:'Examples and feedback'})).className).toMatch(/\buppercase\b/);
  expect(screen.getByText('.note').className).toContain('rounded-full');
  expect(screen.getByText('.memo').className).toContain('rounded-full');
  expect(screen.getByRole('button',{name:'Remove extension .md'}).className).toContain('rounded-full');
  expect(screen.getByRole('switch',{name:'enabled'})).toHaveAttribute('data-state','checked');
  expect(screen.getByLabelText('Example expectation 1')).toHaveValue('valid');
  expect(screen.getByRole('button',{name:'Save definition'}).className).toContain('font-semibold');
 });
 it('saves a toggled boolean and an added extension back into the definition (P04)',async()=>{
  const save=vi.fn();render(<DefinitionForm kind="handler" content={handlerText} readOnly={false} onSave={save}/>);
  fireEvent.click(await screen.findByRole('switch',{name:'enabled'}));
  fireEvent.change(screen.getByLabelText('Add extension'),{target:{value:'.txt'}});
  fireEvent.click(screen.getByRole('button',{name:'Add extension'}));
  fireEvent.click(screen.getByRole('button',{name:'Save definition'}));
  await waitFor(()=>expect(save).toHaveBeenCalled());
  const saved=parseDefinition(save.mock.calls[0][0]);
  expect(saved.enabled).toBe(false);expect(saved.extensions).toEqual(['.md','.txt']);
  expect((saved.examples as {expect:string}[])[0].expect).toBe('valid');
 });
 it('gives a generic definition no examples editor (P04)',async()=>{
  render(<DefinitionForm content={'examples: [{folder: a}]'} readOnly={false} onSave={vi.fn()}/>);
  expect(await screen.findByLabelText('examples')).toBeTruthy();
  expect(screen.queryByRole('heading',{name:'Examples and feedback'})).toBeNull();
 });
 it('lays the viewer sections out with section labels and quiet buttons (P04)',async()=>{
  const {container}=render(<DefinitionViewer path="checks/check.handler" content={handlerText} files={[vf('checks/examples/good/view.renderer','bytes')]} cellId="cell" channel="ch" readOnly={false} onSave={vi.fn()}/>);
  expect((await screen.findByRole('heading',{name:'Example files'})).className).toMatch(/\buppercase\b/);
  expect(screen.getByRole('button',{name:'checks/examples/good/view.renderer'}).className).toContain('font-mono');
  expect(bare(container).map(el=>el.outerHTML.slice(0,80))).toEqual([]);
 });
});
