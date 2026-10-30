import {webcrypto,createHash} from 'node:crypto';
import {beforeEach,describe,expect,it,vi} from 'vitest';
import {act,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {createRef,useEffect} from 'react';
import {FileDocument,type DocumentJournal} from '@/lib/fileDocument';
import type {FileDescriptor} from '@/lib/fileProtocol';
import {textFile} from '@/lib/fileCommands';
import {FullFileEditor,type FullFileEditorHandle} from './FullFileEditor';
import {SAVE_STALL_MS} from '@/hooks/useStalledSave';
import {SHORTCUT_IDS} from '@/lib/keybindings/ids';
const owner=vi.hoisted(()=>({resolution:null as unknown as import('@/hooks/useRendererId').RendererResolution,document:null as FileDocument|null,mounts:0,iframe:null as null|{documentId:string,fileContent:string},shortcuts:new Map<string,(event:KeyboardEvent)=>void>()}));
vi.mock('@/lib/keybindings/useShortcut',()=>({useShortcut:(id:string,callback:(event:KeyboardEvent)=>void)=>{owner.shortcuts.set(id,callback);}}));
vi.mock('@/lib/fileDocuments',()=>({acquireFileDocument:async()=>owner.document,flushFileDocuments:async()=>{}}));
vi.mock('@/hooks/useRendererId',()=>({useRendererId:()=>owner.resolution,refreshRendererCache:vi.fn()}));
vi.mock('./NativeRenderer',()=>({NativeRenderer:({fileContent,onContentChange}:{fileContent:string;onContentChange:(body:string)=>void})=>{
    useEffect(()=>{owner.mounts++;},[]);
    return <textarea aria-label="Editor" value={fileContent} onChange={event=>onContentChange(event.target.value)}/>;
}}));
vi.mock('./SandboxIframe',()=>({SandboxIframe:(props:{documentId:string;fileContent:string})=>{owner.iframe=props;return <div>Iframe</div>;}}));
vi.mock('./StandaloneSandboxIframe',()=>({StandaloneSandboxIframe:()=>null}));
vi.mock('@/lib/rendererFlush',()=>({boundedFlush:(operation:()=>Promise<void>)=>operation(),flushRendererEditors:async()=>{}}));
const metadata:FileDescriptor={path:'notes.txt',file_revision:1,body_hash:'base',encoding:'utf-8',writer_id:'server',edit_seq:1,deleted:false,language:'text'};
const file={name:metadata.path,...metadata};
let send:ReturnType<typeof vi.fn<(operation:import('@/lib/fileProtocol').FileOperation)=>Promise<import('@/lib/fileProtocol').FileMutationResult>>>;
let journal:DocumentJournal;
beforeEach(async()=>{
    vi.clearAllMocks();owner.mounts=0;
    owner.resolution={renderer:{id:'text',name:'Text',extensions:['.txt'],defaultFor:[],embed:'native',contentHash:'renderer',supportsDiff:true,liveRefresh:false},error:null};
    journal={load:async()=>[],save:vi.fn(async()=>{}),remove:vi.fn(async()=>{})};
    vi.stubGlobal('crypto',webcrypto);
    send=vi.fn(async operation=>({operation_id:operation.operation_id,outcome:'accepted',files:[{...metadata,file_revision:operation.mutations[0].base_revision+1,writer_id:'writer',edit_seq:operation.mutations[0].edit_seq,body_hash:createHash('sha256').update(operation.mutations[0].content_text??'').digest('hex')}],conflicts:[]}));
    owner.document=new FileDocument({origin:'https://example.test',channel:'channel',cellId:'cell',path:'notes.txt'},'writer',journal,send,async descriptor=>descriptor.file_revision===1?'original':'external',100000);
    await owner.document.initialize(metadata);
});
const editorProps=()=>({file,onClose:vi.fn(),cellId:'cell',channel:'channel'});
describe('FullFileEditor document lease',()=>{
    it('shows only a failed validation above the renderer',async()=>{
        const props=editorProps();
        const view=render(<FullFileEditor {...props} validationVerdict={{valid:false,error:'Missing required heading'}}/>);
        await screen.findByDisplayValue('original');
        const failure=screen.getByTestId('validation-banner');
        expect(failure).toHaveAttribute('role','alert');
        expect(failure).toHaveTextContent('Validation failed: Missing required heading');
        expect(failure.nextElementSibling).toBe(screen.getByRole('textbox').parentElement);
        view.rerender(<FullFileEditor {...props} validationVerdict={{valid:true}}/>);
        expect(screen.queryByTestId('validation-banner')).toBeNull();
        view.rerender(<FullFileEditor {...props}/>);
        expect(screen.queryByTestId('validation-banner')).toBeNull();
        expect(owner.mounts).toBe(1);
    });
    it('exposes no editable fallback while initial renderer identity is unresolved',async()=>{
        const ready=owner.resolution;
        owner.resolution={renderer:null,error:null};
        const props=editorProps(),view=render(<FullFileEditor {...props}/>);
        await screen.findByText('Loading renderer…');
        expect(screen.queryByRole('textbox')).toBeNull();expect(owner.mounts).toBe(0);
        owner.resolution=ready;view.rerender(<FullFileEditor {...props}/>);
        await screen.findByDisplayValue('original');
        fireEvent.change(screen.getByRole('textbox'),{target:{value:'all typed characters'}});
        view.rerender(<FullFileEditor {...props}/>);
        expect(screen.getByRole('textbox')).toHaveValue('all typed characters');
        expect(owner.mounts).toBe(1);
    });

    it('Escape closes an offline view after local durability and reopening recovers its edits',async()=>{
        send.mockRejectedValue(new Error('server refused'));const props=editorProps();const view=render(<FullFileEditor {...props}/>);
        await screen.findByDisplayValue('original');fireEvent.change(screen.getByRole('textbox'),{target:{value:'offline draft'}});
        await act(async()=>{await expect(owner.document!.flush()).rejects.toThrow('server refused');});
        await act(async()=>{owner.shortcuts.get(SHORTCUT_IDS.EDITOR_CLOSE)!(new KeyboardEvent('keydown',{key:'Escape'}));});
        await waitFor(()=>expect(props.onClose).toHaveBeenCalledTimes(1));expect(send).toHaveBeenCalledTimes(1);
        view.unmount();render(<FullFileEditor {...props}/>);expect(await screen.findByDisplayValue('offline draft')).toBeVisible();
    });
    it('hands every keystroke immediately to the durable owner and reports the local receipt',async()=>{
        let commit!:()=>void;
        const durability=new Promise<void>(resolve=>{commit=resolve;});
        journal.save=vi.fn(()=>durability);
        render(<FullFileEditor {...editorProps()}/>);
        await screen.findByDisplayValue('original');
        fireEvent.change(screen.getByRole('textbox'),{target:{value:'last keystroke'}});
        expect(owner.document!.getSnapshot().body).toBe('last keystroke');
        expect(owner.document!.getSnapshot().status).toBe('Saving locally');
        await act(async()=>{commit();await durability;});
        await waitFor(()=>expect(owner.document!.getSnapshot().status).toBe('Saved locally · waiting for server'));
        expect(journal.save).toHaveBeenCalledWith(expect.objectContaining({body:'last keystroke'}));expect(send).not.toHaveBeenCalled();
        // Saving is silent: no status row, no receipt, nothing reserving space.
        expect(screen.queryByRole('status')).toBeNull();expect(screen.queryByRole('alert')).toBeNull();
    });
    it('retains dirty editor bytes on a new server revision and exposes explicit resolution',async()=>{
        const ref=createRef<FullFileEditorHandle>();render(<FullFileEditor {...editorProps()} ref={ref}/>);
        await screen.findByDisplayValue('original');
        fireEvent.change(screen.getByRole('textbox'),{target:{value:'local'}});
        await act(async()=>{await owner.document!.observe({...metadata,file_revision:2,body_hash:'external'});});
        expect(screen.getByRole('textbox')).toHaveValue('local');expect(screen.getByRole('button',{name:'Review versions'})).toBeVisible();
        expect(owner.mounts).toBe(1);
        fireEvent.click(screen.getByRole('button',{name:'Review versions'}));
        await screen.findByRole('button',{name:'Use server version'});
        fireEvent.click(screen.getByRole('button',{name:'Use server version'}));
        await waitFor(()=>expect(send).toHaveBeenCalled());
        expect(send.mock.calls[0][0].mutations[0]).toMatchObject({content_text:'external',base_revision:2});
    });
    it('closing a view preserves the owner and its journal',async()=>{
        const props=editorProps();const view=render(<FullFileEditor {...props}/>);await screen.findByDisplayValue('original');
        fireEvent.change(screen.getByRole('textbox'),{target:{value:'survives eviction'}});
        await waitFor(()=>expect(journal.save).toHaveBeenCalled());view.unmount();
        expect(owner.document!.getSnapshot().body).toBe('survives eviction');
        render(<FullFileEditor {...props}/>);expect(await screen.findByDisplayValue('survives eviction')).toBeVisible();
    });
    it('flush errors reject silently, then after the stall window an overlay banner says so, retries, and clears once saved',async()=>{
        send.mockRejectedValue(new Error('disk refused'));
        const ref=createRef<FullFileEditorHandle>();render(<FullFileEditor {...editorProps()} ref={ref}/>);await screen.findByDisplayValue('original');
        vi.useFakeTimers({toFake:['setTimeout','clearTimeout','setInterval','clearInterval']});
        try{
            fireEvent.change(screen.getByRole('textbox'),{target:{value:'unsaved'}});
            await act(async()=>{await expect(ref.current!.flushPendingSave()).rejects.toThrow('disk refused');});
            expect(owner.document!.getSnapshot().status).toBe('Server save failed');expect(screen.getByRole('textbox')).toHaveValue('unsaved');
            // Not a word for the first five seconds, and no space reserved for one.
            await act(async()=>{await vi.advanceTimersByTimeAsync(SAVE_STALL_MS-1);});
            expect(screen.queryByRole('alert')).toBeNull();expect(screen.queryByRole('status')).toBeNull();
            // (The document's own one-second checkpoint has retried once by now; the banner adds its own.)
            const before=send.mock.calls.length;
            // Then the banner, over the editor, with the reason, and a retry already in flight.
            await act(async()=>{await vi.advanceTimersByTimeAsync(1);});
            const banner=screen.getByRole('alert');
            expect(banner).toHaveTextContent('Still trying to save your edits.');expect(banner).toHaveTextContent('disk refused');
            expect(banner.className).toContain('absolute');
            expect(send).toHaveBeenCalledTimes(before+1);
            // It keeps trying on the same cadence, and the manual retry works too.
            await act(async()=>{await vi.advanceTimersByTimeAsync(SAVE_STALL_MS);});
            expect(send).toHaveBeenCalledTimes(before+2);
            send.mockImplementation(async operation=>({operation_id:operation.operation_id,outcome:'accepted',files:[{...metadata,file_revision:operation.mutations[0].base_revision+1,writer_id:'writer',edit_seq:operation.mutations[0].edit_seq,body_hash:createHash('sha256').update(operation.mutations[0].content_text??'').digest('hex')}],conflicts:[]}));
            await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Retry now'}));await vi.advanceTimersByTimeAsync(0);});
            expect(send).toHaveBeenCalledTimes(before+3);
            // vi.waitFor advances the fake clock between checks.
            await vi.waitFor(()=>expect(owner.document!.getSnapshot().status).toBe('Saved'));
            await act(async()=>{await vi.advanceTimersByTimeAsync(0);});
            expect(screen.queryByRole('alert')).toBeNull();
        }finally{vi.useRealTimers();}
    });
    it('historical explicit bodies never acquire an editable owner',async()=>{
        render(<FullFileEditor {...editorProps()} file={textFile('history.txt','immutable')} readOnly versionId="version"/>);
        expect(await screen.findByDisplayValue('immutable')).toBeVisible();fireEvent.change(screen.getByRole('textbox'),{target:{value:'refused'}});
        expect(owner.document!.getSnapshot().editSeq).toBe(0);
    });
});
