import {webcrypto} from 'node:crypto';
import {beforeEach,expect,it,vi} from 'vitest';
import {fireEvent,render,screen} from '@testing-library/react';
import {FileDocument,type DocumentJournal} from '@/lib/fileDocument';
import type {FileDescriptor} from '@/lib/fileProtocol';
import {FullFileEditor} from './FullFileEditor';

// The editor hands its renderer's every primary edit to the document owner at
// once, as an exact body or an exact recovery snapshot, each with the view
// that emitted it (no debounce between them).
const owner=vi.hoisted(()=>({document:null as FileDocument|null}));
vi.mock('@/lib/keybindings/useShortcut',()=>({useShortcut:()=>{}}));
vi.mock('@/lib/fileDocuments',()=>({acquireFileDocument:async()=>owner.document,flushFileDocuments:async()=>{}}));
vi.mock('@/hooks/useRendererId',()=>({useRendererId:()=>({renderer:{id:'text',name:'Text',extensions:['.txt'],defaultFor:[],embed:'native',contentHash:'renderer',supportsDiff:true,liveRefresh:false},error:null}),refreshRendererCache:vi.fn()}));
vi.mock('./NativeRenderer',()=>({NativeRenderer:({onContentChange,onSnapshotChange}:{onContentChange:(body:string,view:object)=>void;onSnapshotChange:(snapshot:object,view:object)=>void})=><>
    <button onClick={()=>onContentChange('typed body',{viewId:'view-1',viewSeq:1})}>Type</button>
    <button onClick={()=>onSnapshotChange({codecHash:'codec',snapshotId:'snap-1',payload:{doc:1}},{viewId:'view-1',viewSeq:2})}>Snapshot</button>
</>}));
vi.mock('./SandboxIframe',()=>({SandboxIframe:()=>null}));
vi.mock('./StandaloneSandboxIframe',()=>({StandaloneSandboxIframe:()=>null}));
vi.mock('@/lib/rendererFlush',()=>({boundedFlush:(operation:()=>Promise<void>)=>operation(),flushRendererEditors:async()=>{}}));
const metadata:FileDescriptor={path:'notes.txt',file_revision:1,body_hash:'base',encoding:'utf-8',writer_id:'server',edit_seq:1,deleted:false,language:'text'};
const file={name:metadata.path,...metadata};
beforeEach(async()=>{
    vi.stubGlobal('crypto',webcrypto);
    const journal:DocumentJournal={load:async()=>[],save:vi.fn(async()=>{}),remove:vi.fn(async()=>{})};
    owner.document=new FileDocument({origin:'https://example.test',channel:'channel',cellId:'cell',path:'notes.txt'},'writer',journal,vi.fn(),async()=>'original',100000);
    await owner.document.initialize(metadata);
});

it('hands a renderer\'s body and snapshot edits to the owner at once, with their view',async()=>{
    render(<FullFileEditor file={file} onClose={vi.fn()} cellId="cell" channel="channel"/>);
    fireEvent.click(await screen.findByRole('button',{name:'Type'}));
    expect(owner.document!.getDocumentSnapshot()).toMatchObject({content:'typed body',editSeq:1,viewAcks:{'view-1':1}});
    fireEvent.click(screen.getByRole('button',{name:'Snapshot'}));
    expect(owner.document!.getDocumentSnapshot()).toMatchObject({recovery:{snapshotId:'snap-1',payload:{doc:1}},editSeq:2,viewAcks:{'view-1':2}});
});

