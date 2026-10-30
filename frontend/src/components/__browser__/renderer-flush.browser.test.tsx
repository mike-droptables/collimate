import { fileFixture,fixtureBodyHash } from '@/test/fileFixtures';
import {beforeEach,it, expect, vi } from 'vitest';
import { render } from 'vitest-browser-react';
import { page } from 'vitest/browser';
import { createRef } from 'react';
import { FullFileEditor, type FullFileEditorHandle } from '../FullFileEditor';
import { FIXTURE_FLUSH_ID } from '@/test/rendererFixtures';
import { flushCellSaves, registerCellSaveBarrier } from '@/lib/cellSaveBarrier';
import {getFileDocument} from '@/lib/fileDocuments';
import {useCollimateStore} from '@/stores/useCollimateStore';
import type {FileOperation} from '@/lib/fileProtocol';

const mode=vi.hoisted(()=>({embed:'native'}));
const shortcuts=vi.hoisted(()=>({handlers:{} as Record<string,(event:KeyboardEvent)=>void>}));
const transport=vi.hoisted(()=>({request:vi.fn()}));
vi.mock('@/lib/keybindings/useShortcut',()=>({useShortcut:(id:string,handler:(event:KeyboardEvent)=>void)=>{shortcuts.handlers[id]=handler;}}));
import {SHORTCUT_IDS} from '@/lib/keybindings/ids';
vi.mock('@/hooks/useRendererId',()=>({useRendererId:()=>({renderer:{id:FIXTURE_FLUSH_ID,name:'Flush',extensions:['.txt'],supportsDiff:false,liveRefresh:false,embed:mode.embed,contentHash:'flush'},error:null}),refreshRendererCache:vi.fn()}));
vi.mock('@/lib/api',async original=>({...await original<typeof import('@/lib/api')>(),getApiUrl:()=>`${window.location.origin}/api`}));
vi.mock('@/lib/api/client',async original=>({...await original<typeof import('@/lib/api/client')>(),getApiUrl:()=>`${window.location.origin}/api`,requestJson:transport.request}));
vi.mock('@/lib/cellFileCap',async original=>({...await original<typeof import('@/lib/cellFileCap')>(),cellFileCap:async()=> 'testcap'}));
beforeEach(()=>{
  mode.embed='native';transport.request.mockReset();
  transport.request.mockImplementation(async(_path:string,method:string,operation?:FileOperation)=>{
    if(method==='GET')return {conflicts:[]};
    if(method!=='POST'||!operation)throw new Error('Unexpected fixture transport');
    return {operation_id:operation.operation_id,outcome:'accepted',conflicts:[],files:operation.mutations.map(mutation=>({
      path:mutation.path,file_revision:mutation.base_revision+1,body_hash:fixtureBodyHash(mutation.content_text??''),
      language:mutation.language??'plaintext',encoding:'utf-8',deleted:false,writer_id:mutation.writer_id,edit_seq:mutation.edit_seq,
    }))};
  });
});
function ownedFile(name:string){
  const cellId=crypto.randomUUID(),channel='cccccccc-cccc-4ccc-8ccc-cccccccccccc',file=fileFixture(name,'original');
  useCollimateStore.setState({cellCache:{...useCollimateStore.getState().cellCache,[cellId]:{id:cellId,name:'Flush',files:[file]}}});
  return {cellId,channel,file};
}
function acceptedBody(body:string){
  expect(transport.request).toHaveBeenCalledWith(expect.stringContaining('/files/mutate'),'POST',expect.objectContaining({
    mutations:[expect.objectContaining({kind:'put',content_text:body})],
  }));
}

for (const action of ['Save','Run'] as const) {
  it(`drains actual mounted SDK immediate edits before ${action} __F0691_F1808_F2003`,async()=>{
    const ref=createRef<FullFileEditorHandle>(),props=ownedFile('notes.txt');
    const view=await render(<FullFileEditor ref={ref} {...props} onClose={()=>{}} />);
    const editor=page.getByRole('textbox',{name:'SDK pending editor'});
    await expect.element(editor).toHaveValue('original');
    await editor.fill('newest pending text');
    const off=registerCellSaveBarrier(props.cellId,()=>ref.current!.flushPendingSave());
    try {
      if(action==='Run') await flushCellSaves(props.cellId);
      else await ref.current!.flushPendingSave();
      acceptedBody('newest pending text');
      const snapshot=getFileDocument(props.channel,props.cellId,props.file.name)!.getSnapshot();
      expect(snapshot.savedSeq).toBe(snapshot.editSeq);
    }finally{off();await view.unmount();}
  });
}


it('keeps the actual iframe alive until its pending SDK text is serialized before close __F0694_F2003',async()=>{
  mode.embed='iframe';
  const ref=createRef<FullFileEditorHandle>(),props=ownedFile('iframe.txt');
  const view=await render(<FullFileEditor ref={ref} fullscreen {...props} onClose={()=>{}} />);
  try {
    let field:HTMLTextAreaElement|null=null;
    await vi.waitFor(()=>{
      const frame=document.querySelector('iframe');
      field=frame?.contentDocument?.querySelector('textarea') || null;
      expect(field?.value).toBe('original');
    },{timeout:15000});
    const frame=field!.ownerDocument.defaultView! as Window & typeof globalThis;
    Object.getOwnPropertyDescriptor(frame.HTMLTextAreaElement.prototype,'value')!.set!.call(field,'iframe pending text');
    field!.dispatchEvent(new frame.Event('input',{bubbles:true}));
    const iframeElement=frame.frameElement!;
    await ref.current!.flushPendingSave();
    acceptedBody('iframe pending text');
    expect(field!.isConnected).toBe(true);
    await view.unmount();
    expect(iframeElement.isConnected).toBe(false);
  } finally {mode.embed='native';await view.unmount();}
});


it('drains actual SDK immediate edits before a forced native unmount __F1808',async()=>{
  const props=ownedFile('unmount.txt');
  const view=await render(<FullFileEditor {...props} onClose={()=>{}} />);
  const editor=page.getByRole('textbox',{name:'SDK pending editor'});
  await expect.element(editor).toHaveValue('original');
  await editor.fill('pending before unmount');
  await view.unmount();
  const owner=getFileDocument(props.channel,props.cellId,props.file.name)!;
  await owner.locallyDurable();
  expect(owner.getSnapshot().body).toBe('pending before unmount');
  expect(owner.getSnapshot().localSeq).toBe(owner.getSnapshot().editSeq);
});


it('awaits actual iframe asynchronous callbacks before removing the document __F0691_F0694_F2003',async()=>{
  mode.embed='iframe';
  const ref=createRef<FullFileEditorHandle>(),props=ownedFile('async.txt');
  let closed=false;
  const close=vi.fn(async()=>{closed=true;await view.unmount();});
  const view=await render(<FullFileEditor ref={ref} fullscreen {...props} onClose={close} />);
  let release!:()=>void;
  try {
    let field:HTMLTextAreaElement|null=null;
    await vi.waitFor(()=>{field=document.querySelector('iframe')?.contentDocument?.querySelector('textarea') || null;expect(field?.value).toBe('original');},{timeout:15000});
    const frame=field!.ownerDocument.defaultView! as Window & typeof globalThis;
    const iframeElement=frame.frameElement!;
    Object.getOwnPropertyDescriptor(frame.HTMLTextAreaElement.prototype,'value')!.set!.call(field,'serialized latest');
    field!.dispatchEvent(new frame.Event('input',{bubbles:true}));
    await vi.waitFor(()=>expect((frame as unknown as {__fixtureFlushReady:boolean}).__fixtureFlushReady).toBe(true));
    (frame as unknown as {__fixtureFlushGate:Promise<void>}).__fixtureFlushGate=new Promise<void>(resolve=>{release=resolve;});
    shortcuts.handlers[SHORTCUT_IDS.EDITOR_CLOSE](new KeyboardEvent('keydown',{key:'Escape'}));
    await new Promise(resolve=>setTimeout(resolve,30));
    expect(closed).toBe(false);expect(iframeElement.isConnected).toBe(true);
    await vi.waitFor(()=>expect(getFileDocument(props.channel,props.cellId,props.file.name)!.getSnapshot().body).toBe('serialized latest'));
    release();await vi.waitFor(()=>expect(close).toHaveBeenCalledTimes(1));
    const owner=getFileDocument(props.channel,props.cellId,props.file.name)!;
    expect(owner.getSnapshot().localSeq).toBe(owner.getSnapshot().editSeq);
    expect(iframeElement.isConnected).toBe(false);
  }finally{release?.();mode.embed='native';await view.unmount();}
});
