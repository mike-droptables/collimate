import {afterEach,beforeEach,expect,it,vi} from 'vitest';
const primitives=vi.hoisted(()=>({compilePreview:vi.fn(),preparePreviewDependencies:vi.fn(),rendererFilePaths:vi.fn()}));
const surface=vi.hoisted(()=>({rendererSurface:vi.fn()}));
vi.mock('./rendererSurface',()=>surface);
vi.mock('./rendererPreview',()=>primitives);
vi.mock('./rendererBundle',()=>({answerRendererBundleRequest:vi.fn()}));
vi.mock('./api',()=>({cellFileUrl:vi.fn()}));
vi.mock('./currentChannel',()=>({currentChannelId:()=> 'current'}));
vi.mock('./siblingWrite',()=>({saveSiblingFileInCell:vi.fn()}));
vi.mock('./revealFile',()=>({revealCellFile:vi.fn()}));
vi.mock('./keybindings/forwardedCombos',()=>({buildForwardedCombos:()=>[]}));
const theme=vi.hoisted(()=>({snapshot:{themeId:'paper',scheme:'light',known:true,vars:{'--sheet-2':'#f1efea','--font-prose':'"Alegreya", serif'},fonts:{ui:'u',mono:'m',prose:'"Alegreya", serif'}}}));
vi.mock('./themeSnapshot',()=>({readThemeSnapshot:()=>theme.snapshot}));
const editing=vi.hoisted(()=>({snapshot:{spellcheck:false}}));
vi.mock('./editingSnapshot',()=>({readEditingSnapshot:()=>editing.snapshot}));
const store=vi.hoisted(()=>({setOsFileDragActive:vi.fn()}));
vi.mock('@/stores/useCollimateStore',()=>({useCollimateStore:{getState:()=>store}}));
import {handleSandboxHostMessage,pushSandboxEditing,pushSandboxTheme} from './sandboxBridge';
import {revealCellFile} from './revealFile';
beforeEach(()=>{vi.stubGlobal('window',{location:{origin:'http://localhost'}});vi.clearAllMocks();});
afterEach(()=>vi.unstubAllGlobals());
it('routes iframe membership only for the current owned source and immutable version __D14',async()=>{
 const target={postMessage:vi.fn()} as unknown as Window;
 const context={target,isCurrent:()=>true,ready:vi.fn(),rendererId:'renderer',fileName:'file',fileHash:'hash',channel:'channel',cellId:'cell',versionId:'version'};
 const data={type:'FILE_PATHS',id:4};primitives.rendererFilePaths.mockResolvedValue(['one']);
 handleSandboxHostMessage({data,source:{},origin:window.location.origin} as MessageEvent,context);
 handleSandboxHostMessage({data,source:target,origin:'https://foreign.invalid'} as MessageEvent,context);
 expect(primitives.rendererFilePaths).not.toHaveBeenCalled();
 handleSandboxHostMessage({data,source:target,origin:window.location.origin} as MessageEvent,context);
 await vi.waitFor(()=>expect(target.postMessage).toHaveBeenCalledWith({type:'PREVIEW_PRIMITIVE_RESULT',id:4,value:['one']},window.location.origin));
 expect(primitives.rendererFilePaths).toHaveBeenCalledWith('channel','cell','version');
});
it('drops late compiler output when its iframe owner is replaced __D14',async()=>{
 const target={postMessage:vi.fn()} as unknown as Window;let current=true;let finish!:(value:unknown)=>void;
 primitives.compilePreview.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
 const context={target,isCurrent:()=>current,ready:vi.fn(),rendererId:'renderer',fileName:'file',fileHash:'hash'};
 const files={'main.ts':new Uint8Array([1])};
 handleSandboxHostMessage({source:target,origin:window.location.origin,data:{type:'COMPILE_RENDERER',id:9,files,entrypoint:'main.ts'}} as MessageEvent,context);
 expect(primitives.compilePreview).toHaveBeenCalledWith(files,'main.ts');current=false;finish({code:'unapproved'});await Promise.resolve();
 expect(target.postMessage).not.toHaveBeenCalled();
});
it('looks up only the mounted surface and drops a replaced owner reply __D22',async()=>{
 const target={postMessage:vi.fn()} as unknown as Window;let current=true;let finish!:(value:unknown)=>void;
 surface.rendererSurface.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
 const context={target,isCurrent:()=>current,ready:vi.fn(),rendererId:'renderer',fileName:'physical/board.panel',fileHash:'hash',channel:'channel',cellId:'cell'};
 const data={type:'GET_SURFACE',id:12,path:'attacker.panel',cellId:'other'};
 handleSandboxHostMessage({source:{},origin:window.location.origin,data} as MessageEvent,context);
 expect(surface.rendererSurface).not.toHaveBeenCalled();
 handleSandboxHostMessage({source:target,origin:window.location.origin,data} as MessageEvent,context);
 expect(surface.rendererSurface).toHaveBeenCalledWith('channel','cell','physical/board.panel',undefined);
 current=false;finish({chat_id:'stale'});await Promise.resolve();
 expect(target.postMessage).not.toHaveBeenCalled();
});
it('preserves history lookup context and returns surface errors __D22',async()=>{
 const target={postMessage:vi.fn()} as unknown as Window;
 const context={target,isCurrent:()=>true,ready:vi.fn(),rendererId:'renderer',fileName:'board.panel',fileHash:'hash',channel:'channel',cellId:'cell',versionId:'v1'};
 surface.rendererSurface.mockRejectedValue(new Error('host unavailable'));
 handleSandboxHostMessage({source:target,origin:window.location.origin,data:{type:'GET_SURFACE',id:13}} as MessageEvent,context);
 expect(surface.rendererSurface).toHaveBeenCalledWith('channel','cell','board.panel','v1');
 await vi.waitFor(()=>expect(target.postMessage).toHaveBeenCalledWith({type:'PREVIEW_PRIMITIVE_RESULT',id:13,error:'host unavailable'},window.location.origin));
});

it('P4F04_P5F05 preparation belongs to its exact iframe owner and cancellation aborts inert downloads',async()=>{
 const target={postMessage:vi.fn()} as unknown as Window;let signal:AbortSignal|undefined;let finish!:()=>void;
 primitives.preparePreviewDependencies.mockImplementation((_files,_entrypoint,next)=>{signal=next;return new Promise<void>(resolve=>{finish=resolve;});});
 const context={target,isCurrent:()=>true,ready:vi.fn(),rendererId:'renderer',fileName:'file',fileHash:'hash'};
 handleSandboxHostMessage({source:{},origin:window.location.origin,data:{type:'PREPARE_RENDERER_DEPENDENCIES',id:7}} as MessageEvent,context);
 expect(primitives.preparePreviewDependencies).not.toHaveBeenCalled();
 handleSandboxHostMessage({source:target,origin:window.location.origin,data:{type:'PREPARE_RENDERER_DEPENDENCIES',id:7,files:{},entrypoint:'main.js'}} as MessageEvent,context);
 expect(signal?.aborted).toBe(false);
 handleSandboxHostMessage({source:target,origin:window.location.origin,data:{type:'CANCEL_PREPARE_RENDERER_DEPENDENCIES',id:7}} as MessageEvent,context);
 expect(signal?.aborted).toBe(true);finish();await Promise.resolve();
});

it('posts the theme snapshot to the frame unchanged, keyed by CSS name, and nothing else (S01)',()=>{
 const target={postMessage:vi.fn()} as unknown as Window;
 pushSandboxTheme(target);
 expect(target.postMessage).toHaveBeenCalledExactlyOnceWith({type:'SET_THEME',...theme.snapshot},window.location.origin);
});

it('an OS file drag that entered the window over the frame raises the host\'s flag, from its own frame only',()=>{
 const target={postMessage:vi.fn()} as unknown as Window;
 const context={target,isCurrent:()=>true,ready:vi.fn(),rendererId:'renderer',fileName:'file',fileHash:'hash'};
 handleSandboxHostMessage({source:{},origin:window.location.origin,data:{type:'OS_FILE_DRAG'}} as MessageEvent,context);
 handleSandboxHostMessage({source:target,origin:'https://foreign.invalid',data:{type:'OS_FILE_DRAG'}} as MessageEvent,context);
 expect(store.setOsFileDragActive).not.toHaveBeenCalled();
 handleSandboxHostMessage({source:target,origin:window.location.origin,data:{type:'OS_FILE_DRAG'}} as MessageEvent,context);
 expect(store.setOsFileDragActive).toHaveBeenCalledExactlyOnceWith(true);
});

it('posts the editing preferences as SET_EDITING, the record api.getEditing returns',()=>{
 const target={postMessage:vi.fn()} as unknown as Window;
 pushSandboxEditing(target);
 expect(target.postMessage).toHaveBeenCalledExactlyOnceWith({type:'SET_EDITING',spellcheck:false},window.location.origin);
});
it('reveals a file of its own cell, in a new tab only when the renderer asks for one',()=>{
 const target={postMessage:vi.fn()} as unknown as Window;
 const context={target,isCurrent:()=>true,ready:vi.fn(),rendererId:'milkdown',fileName:'notes.md',fileHash:'hash',channel:'channel',cellId:'cell'};
 const reveal=(data:Record<string,unknown>,source:unknown=target)=>handleSandboxHostMessage({source,origin:window.location.origin,data:{type:'REVEAL_FILE',...data}} as MessageEvent,context);
 reveal({path:'data.csv'});
 expect(revealCellFile).toHaveBeenLastCalledWith('cell','data.csv',{newTab:false});
 reveal({path:'data.csv',newTab:true});
 expect(revealCellFile).toHaveBeenLastCalledWith('cell','data.csv',{newTab:true});
 // Only a literal true asks for a new tab.
 reveal({path:'data.csv',newTab:'yes'});
 expect(revealCellFile).toHaveBeenLastCalledWith('cell','data.csv',{newTab:false});
 // Another frame's post reveals nothing.
 reveal({path:'data.csv',newTab:true},{});
 expect(revealCellFile).toHaveBeenCalledTimes(3);
});
