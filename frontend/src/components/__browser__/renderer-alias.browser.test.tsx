import {fixtureText,fileFixture} from '@/test/fileFixtures';
import {useCollimateStore} from '@/stores/useCollimateStore';
import {expect,it,vi} from 'vitest';
import {render} from 'vitest-browser-react';
import {NativeRenderer} from '../NativeRenderer';
import {StandaloneSandboxIframe} from '../StandaloneSandboxIframe';
import {setRendererHandlers} from '@/lib/rendererAliases';
import {uploadBlob} from '@/lib/api';
import {writeCellBlob} from '@/lib/fileCommands';
import type {DocumentViewEdit} from '@/lib/fileDocument';
vi.mock('@/lib/fileCommands',()=>({writeCellBlob:vi.fn(async()=>undefined),applyMutationResult:vi.fn(),recoverFileOperations:vi.fn(async()=>{}),replaceCellFiles:vi.fn(),submitFileMutations:vi.fn(),moveCellFiles:vi.fn(),expectedFileRevision:vi.fn(),hydrateFiles:vi.fn(),currentCellFiles:vi.fn(),textFile:vi.fn(),binaryFile:vi.fn(),withBody:vi.fn(),needsTextBody:vi.fn(),readRendererSiblingText:vi.fn(async(_channel:string,cellId:string,path:string)=>fixtureText(useCollimateStore.getState().cellCache[cellId]?.files.find(file=>file.name===path))),readCellText:vi.fn(async(_channel:string,cellId:string,path:string)=>fixtureText(useCollimateStore.getState().cellCache[cellId]?.files.find(file=>file.name===path)))}));
import {saveSiblingFileInCell} from '@/lib/siblingWrite';
vi.mock('@/lib/api',async original=>({...await original<typeof import('@/lib/api')>(),getApiUrl:()=>`${window.location.origin}/api`,uploadBlob:vi.fn(async()=>({blob_hash:'staged-binary',size:2}))}));
vi.mock('@/lib/cellFileCap',async original=>({...await original<typeof import('@/lib/cellFileCap')>(),cellFileCap:async()=> 'testcap'}));
vi.mock('@/lib/siblingWrite',()=>({saveSiblingFileInCell:vi.fn(async()=>({ok:true}))}));
type AliasApi={stageDraftFile:(path:string,bytes:Uint8Array)=>Promise<boolean>;getFilename:()=>string;getSiblingFile:(path:string)=>Promise<string>;getFileUrl:(path:string)=>string;saveSiblingFile:(path:string,text:string)=>Promise<boolean>;reportChange:(text:string)=>void};
type ProbeWindow=Window & {__aliasApi?:AliasApi;__aliasSubscribed?:AliasApi;__aliasContent?:string;__aliasEvents?:Array<{cellId:string;changedFiles:string[]}>};
for(const embed of ['native','iframe'] as const) {
 it(`maps own aliased reads, saves, URLs and live changes without renaming storage in ${embed} __D11_P4F09`,async()=>{
  setRendererHandlers([{types:[{ext:'.report_html',render_as:'.html'}]}]);
  vi.mocked(saveSiblingFileInCell).mockClear();vi.mocked(uploadBlob).mockClear();vi.mocked(writeCellBlob).mockClear();
  useCollimateStore.setState({cellCache:{'alias-cell':{id:'alias-cell',name:'Alias',files:[fileFixture('folder/page.report_html','file:folder/page.report_html'),fileFixture('other.html','file:other.html')]}}});
  const reads:string[]=[];const updates:string[]=[];
  const original=window.fetch.bind(window);
  const fetchSpy=vi.spyOn(window,'fetch').mockImplementation(async(input,init)=>{
   const url=String(input);
   if(url.includes('/cells/alias-cell/files/')){reads.push(decodeURIComponent(url.split('/files/')[1]));return new Response('file:'+reads.at(-1));}
   return original(input,init);
  });
  const viewAcks:Record<string,number>={};
  const props={rendererId:'fixture-renderer-alias',rendererHash:'alias',fileName:'folder/page.report_html',cellId:'alias-cell',channel:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',isActiveFocus:true,
   documentSnapshot:{content:'original',editSeq:0,materializedSeq:0,viewAcks:{}},
   onContentChange:(body:string,view?:DocumentViewEdit)=>{updates.push(body);if(view)viewAcks[view.viewId]=view.viewSeq;}};
  const content=URL.createObjectURL(new Blob(['original']));
  const view=await render(embed==='native'?<NativeRenderer {...props} fileContent='original'/>:<StandaloneSandboxIframe {...props} fileHash={content} fileContent='original'/>);
  try {
   let realm:ProbeWindow;
   await vi.waitFor(()=>{realm=(embed==='native'?window:document.querySelector('iframe')?.contentWindow) as ProbeWindow;expect(realm?.__aliasApi).toBeDefined();expect(realm?.__aliasContent).toBe('original');},{timeout:10000});
   const api=realm!.__aliasApi!;
   expect(api.getFilename()).toBe('folder/page.html');
   expect(await api.getSiblingFile(api.getFilename())).toBe('file:folder/page.report_html');
   expect(await api.getSiblingFile('other.html')).toBe('file:other.html');
   expect(reads).toEqual([]);
   expect(api.getFileUrl(api.getFilename())).toContain('/files/folder/page.report_html');
   expect(api.getFileUrl('other.html')).toContain('/files/other.html');
   expect(await api.saveSiblingFile(api.getFilename(),'own saved')).toBe(true);
   expect(await api.saveSiblingFile('other.html','sibling saved')).toBe(true);
   expect(vi.mocked(saveSiblingFileInCell).mock.calls.map(call=>call.slice(0,3))).toEqual([
    ['alias-cell','folder/page.report_html','own saved'],['alias-cell','other.html','sibling saved'],
   ]);
   if(embed==='native') {
    const bytes=new Uint8Array([0,255]);
    expect(await api.stageDraftFile(api.getFilename(),bytes)).toBe(true);
    expect(await api.stageDraftFile('other.bin',bytes)).toBe(true);
    expect(vi.mocked(uploadBlob).mock.calls).toEqual([[bytes],[bytes]]);
    expect(vi.mocked(writeCellBlob).mock.calls).toEqual([
     ['aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa','alias-cell','folder/page.report_html','staged-binary'],
     ['aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa','alias-cell','other.bin','staged-binary'],
    ]);
   }
   api.reportChange('primary saved');
   await vi.waitFor(()=>expect(updates).toContain('primary saved'));
   const changedFiles=['folder/page.report_html','other.html'];
   await vi.waitFor(()=>expect(realm!.__aliasSubscribed).toBe(api),{timeout:15000});
   window.dispatchEvent(new CustomEvent('collimate:files-changed-broadcast',{detail:{cellId:'alias-cell',changedFiles}}));
   await vi.waitFor(()=>expect(realm!.__aliasEvents).toContainEqual({cellId:'alias-cell',changedFiles:['folder/page.html','other.html']}));
   expect(changedFiles).toEqual(['folder/page.report_html','other.html']);
   // Metadata notifications do not fetch or overwrite the document. The
   // owner explicitly pushes the accepted body into either host.
   expect(realm!.__aliasContent).toBe('original');
   const replacement={content:'file:folder/page.report_html',editSeq:1,materializedSeq:1,viewAcks:{...viewAcks}};
   expect(Object.values(viewAcks)).toEqual([1]);
   await view.rerender(embed==='native'?<NativeRenderer {...props} documentSnapshot={replacement} fileContent={replacement.content}/>:<StandaloneSandboxIframe {...props} documentSnapshot={replacement} fileHash={content} fileContent={replacement.content}/>);
   await vi.waitFor(()=>expect(realm!.__aliasContent).toBe('file:folder/page.report_html'));
   expect(reads).toEqual([]);
   // A registry refresh changes the displayed path but preserves the actual
   // write owner and file bytes. Both host kinds remount the current mapping.
   setRendererHandlers([{types:[{ext:'.report_html',render_as:'.md'}]}]);
   await vi.waitFor(()=>{
    realm=(embed==='native'?window:document.querySelector('iframe')?.contentWindow) as ProbeWindow;
    expect(realm?.__aliasApi?.getFilename()).toBe('folder/page.md');
   },{timeout:10000});
   expect(realm!.__aliasApi!.getFileUrl('folder/page.md')).toContain('/files/folder/page.report_html');
  }finally{await view.unmount();URL.revokeObjectURL(content);fetchSpy.mockRestore();setRendererHandlers([]);}
 });
}

for(const embed of ['native','iframe'] as const) {
 it.each([false,true])(`gets live registration for the physical mounted file in ${embed}, history=%s __D22`,async(history)=>{
  setRendererHandlers([{types:[{ext:'.report_html',render_as:'.html'}]}]);
  const reads:string[]=[];const session={state:'live',chat_id:'live',ws_path:'/ws/live',token:'session-token',label:'Review',run_id:'run'};
  const original=window.fetch.bind(window);
  const fetchSpy=vi.spyOn(window,'fetch').mockImplementation(async(input,init)=>{
   const url=String(input);
   if(url.includes('/surfaces/')){reads.push(url);return new Response(JSON.stringify(session),{headers:{'Content-Type':'application/json'}});}
   return original(input,init);
  });
  const props={rendererId:'fixture-renderer-alias',rendererHash:'surface-api',fileName:'folder/page.report_html',cellId:'alias-cell',channel:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',isActiveFocus:true,versionId:history?'snapshot':undefined};
  const content=URL.createObjectURL(new Blob(['surface']));
  const view=await render(embed==='native'?<NativeRenderer {...props} fileContent='surface'/>:<StandaloneSandboxIframe {...props} fileHash={content}/>);
  let off:(()=>void)|undefined;
  try{
   let realm:ProbeWindow;
   await vi.waitFor(()=>{realm=(embed==='native'?window:document.querySelector('iframe')?.contentWindow) as ProbeWindow;expect(realm?.__aliasApi).toBeDefined();},{timeout:10000});
   const api=realm!.__aliasApi! as AliasApi & {getSurface:()=>Promise<typeof session|null>;onSurfaceChange:(callback:()=>void)=>()=>void};
   expect(await api.getSurface()).toEqual(history?null:session);
   expect(reads).toEqual(history?[]:[expect.stringContaining('/cells/alias-cell/surfaces/folder/page.report_html')]);
   let changes=0;off=api.onSurfaceChange(()=>{changes++;});
   for(const detail of [{channel:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',cellId:'other',path:props.fileName},{channel:'other',cellId:'alias-cell',path:props.fileName},{channel:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',cellId:'alias-cell',path:'folder/page.html'}])
    window.dispatchEvent(new CustomEvent('collimate:surface-changed',{detail}));
   window.dispatchEvent(new CustomEvent('collimate:surface-changed',{detail:{channel:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',cellId:'alias-cell',path:props.fileName}}));
   if(history){await new Promise(resolve=>setTimeout(resolve,60));expect(changes).toBe(0);}
   else await vi.waitFor(()=>expect(changes).toBe(1));
   off();off=undefined;
   window.dispatchEvent(new CustomEvent('collimate:surface-changed',{detail:{channel:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',cellId:'alias-cell',path:props.fileName}}));
   await new Promise(resolve=>setTimeout(resolve,60));expect(changes).toBe(history?0:1);
  }finally{off?.();await view.unmount();URL.revokeObjectURL(content);fetchSpy.mockRestore();setRendererHandlers([]);}
 });
}
