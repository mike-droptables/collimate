import type {DocumentViewEdit,DocumentViewSnapshot} from '@/lib/fileDocument';
import type {RendererRecoverySnapshot} from '@/lib/fileSnapshotCodec';
import {diffLines} from '@/lib/textDiff';
import {BinaryConflictReview} from './BinaryConflictReview';
import {isRunPath} from '@/lib/runningCell';
import '@/lib/rendererPreviewEvidence';
import React, { useState, useEffect, useCallback } from 'react';
import type { VirtualFile } from '@/lib/types';
import { cn } from '@/lib/utils';
import { btn, dialog } from '@/components/ui/formStyles';
import { currentChannelId } from '@/lib/currentChannel';
import { useShortcut } from '@/lib/keybindings/useShortcut';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { SandboxIframe } from './SandboxIframe';
import { StandaloneSandboxIframe } from './StandaloneSandboxIframe';
import { NativeRenderer } from './NativeRenderer';
import { NativeRendererErrorBoundary } from './NativeRendererErrorBoundary';
import { useRendererId, refreshRendererCache } from '@/hooks/useRendererId';
import { useFileDocument } from '@/hooks/useFileDocument';
import { useStalledSave } from '@/hooks/useStalledSave';
import { readPinnedText, descriptorOf } from '@/lib/fileBodies';
import { cellFileUrl } from '@/lib/api';
import { boundedFlush, flushRendererEditors } from '@/lib/rendererFlush';
import type { UpdateFiles } from '@/lib/fileTransfer';
import type { CellValidation } from '@/lib/definitionApi';
const DefinitionViewer = React.lazy(() => import('./definitions/DefinitionViewer'));
export interface FullFileEditorHandle {
    /** Await renderer serialization and the current document durability receipt. */
    flushPendingSave: () => Promise<void>;
}

interface FullFileEditorProps {
    files?: VirtualFile[];
    onUpdateFiles?: UpdateFiles;
    onOpenFile?: (path: string) => void;
    file: VirtualFile | import('@/lib/types').FileBody;
    onClose: () => void;
    readOnly?: boolean;
    isActive?: boolean;
    cellId: string;
    versionId?: string;
    channel?: string;
    /** Called after the user explicitly invokes save (Cmd+S). The
     *  CellEditor wires this to its `saveNow` so the cell-level Save
     *  flow (await the current durable document receipt)
     *  runs from the shortcut path too, not just the on-screen button. */
    onAfterSave?: () => void;
    /** When provided, this editor renders a per-file diff: the string is
     *  the "before" (base/target) content for THIS file. Diff mode is
     *  derived purely from this prop — `undefined` means "no diff, show
     *  the file plainly"; `""` is a legitimate diff against an empty file
     *  and DOES enable diff. The diff base is supplied explicitly by the
     *  caller (cell-history slider, cell-pair diff) rather than looked
     *  up from a pinned live cell. */
    diffBaseContent?: string;
    /** Human names for the diff's two sides (e.g. "v3"/"v4" in the version-
     *  history slider). Surfaced to renderers via api.getDiffLabels() so
     *  their diff views can title the panes instead of generic
     *  "Before"/"After". Only meaningful alongside diffBaseContent. */
    diffLabels?: { base: string; current: string };
    /** When true, this editor is mounted in the fullscreen overlay (a
     *  body-level portal above the canvas). Iframe-embed renderers normally
     *  borrow a pooled <iframe> that lives inside the canvas transform and
     *  would be occluded by the overlay, so we swap them for a standalone,
     *  non-pooled iframe here. Native (shadow-DOM) renderers paint in place
     *  and are unaffected. */
    fullscreen?: boolean;
    /** Gates this editor's window-level Esc/Save shortcuts. Defaults to true.
     *  Set false for an inline editor sitting *behind* the fullscreen overlay —
     *  otherwise its EDITOR_CLOSE listener would also fire on the Esc that
     *  dismisses the overlay and silently close the other split pane's file. */
    editorShortcutsEnabled?: boolean;
    /** False while the host cell is scrolled offscreen and display:none'd, or
     *  while this tab isn't its leaf's active tab (which also clears GPU/canvas
     *  drawing buffers). Forwarded to NativeRenderer so GPU-backed renderers
     *  repaint on re-show. Defaults to true for always-visible mounts
     *  (maximized overlay, diff browser). */
    visible?: boolean;
    /** "Open with" override — render with this renderer instead of the primary
     *  resolution for the file's extension. Falls back to the primary when the
     *  id no longer resolves (e.g. its source cell was deleted). */
    rendererId?: string;
    /** Current validation check for the open live file. */
    validationVerdict?: CellValidation['verdicts'][string];
}

export const FullFileEditor = React.forwardRef<FullFileEditorHandle, FullFileEditorProps>(({ file, files = [], onUpdateFiles, onOpenFile, onClose, readOnly: requestedReadOnly = false, isActive, cellId, versionId, channel, onAfterSave, diffBaseContent, diffLabels, fullscreen = false, editorShortcutsEnabled = true, visible = true, rendererId, validationVerdict }, ref) => {
    const readOnly=requestedReadOnly||diffBaseContent!==undefined||isRunPath(file.name);
    const {renderer,error:rendererError} = useRendererId(file.name, rendererId);
    const fileChannel=channel || currentChannelId();
    const editable=!readOnly && !versionId && file.encoding!=='binary';
    const {document,snapshot,error}=useFileDocument(fileChannel,cellId,file,editable);
    const [readBody,setReadBody]=useState<string|null>('content' in file?file.content:null);
    const [readError,setReadError]=useState<string|null>(null);
    const [saveError,setSaveError]=useState<string|null>(null);
    const [competing,setCompeting]=useState<string|null>(null);
    const [review,setReview]=useState<{local:string;seq:number;revision:number;server:string;candidates:{label:string;body:string}[]}|null>(null);
    const [leftVersion,setLeftVersion]=useState(0),[rightVersion,setRightVersion]=useState(1);
    const [merged,setMerged]=useState<string>('');
    useEffect(()=>{
        if(editable)return;
        let active=true;setReadError(null);
        if('content' in file){setReadBody(file.content);return;}
        if(file.encoding==='binary'){setReadBody('');return;}
        setReadBody(null);
        const pending=versionId?fetch(cellFileUrl(fileChannel,cellId,file.name,versionId)).then(response=>{if(!response.ok)throw new Error(`HTTP ${response.status}`);return response.text();}):readPinnedText(fileChannel,cellId,descriptorOf(file));
        void pending.then(body=>{if(active)setReadBody(body);},e=>{if(active)setReadError(String(e?.message??e));});
        return()=>{active=false;};
    },[editable,file.name,file.body_hash,file.file_revision,fileChannel,cellId,versionId]);
    const content=editable?snapshot.body:readBody;
    const change=useCallback((body:string,view?:DocumentViewEdit)=>{if(readOnly||versionId)return;if(document)document.edit(body,false,view);},[document,readOnly,versionId]);
    const snapshotChange=useCallback((recovery:RendererRecoverySnapshot,view?:DocumentViewEdit)=>{if(!readOnly&&!versionId)document?.editSnapshot(recovery,view);},[document,readOnly,versionId]);
    const getContent=useCallback(()=>document?document.materializedBody():Promise.resolve(content??''),[document,content]);
    const rendererSnapshot:DocumentViewSnapshot|undefined=document?{ownerId:document.instanceId,content:snapshot.body??'',recovery:snapshot.recovery,editSeq:snapshot.editSeq,materializedSeq:snapshot.materializedSeq??snapshot.editSeq,viewAcks:snapshot.viewAcks??{}}:undefined;
    const flushPendingSave=useCallback(()=>boundedFlush(async()=>{
        await flushRendererEditors(cellId,file.name);
        if(document)await document.retry();
    }).then(()=>setSaveError(null),e=>{setSaveError(String(e?.message??e));throw e;}),[document,cellId,file.name]);
    React.useImperativeHandle(ref,()=>({flushPendingSave}),[flushPendingSave]);
    const prepareClose=useCallback(()=>boundedFlush(async()=>{
        await flushRendererEditors(cellId,file.name);await document?.locallyDurable();
    }).catch(e=>{setSaveError(String(e?.message??e));throw e;}),[document,cellId,file.name]);
    useShortcut(SHORTCUT_IDS.EDITOR_CLOSE,e=>{e.preventDefault();void prepareClose().then(onClose).catch(()=>undefined);},{target:'window',skipWhenTyping:false,enabled:editorShortcutsEnabled});
    useShortcut(SHORTCUT_IDS.EDITOR_SAVE,e=>{e.preventDefault();void flushPendingSave().then(()=>onAfterSave?.()).catch(()=>undefined);},{target:'window',skipWhenTyping:false,enabled:editorShortcutsEnabled});
    const resolve=async(body:string)=>{if(!document)return;try{await document.resolve(body,review?.seq,review?.revision);setReview(null);setCompeting(null);setSaveError(null);}catch(e){setSaveError(String((e as Error)?.message??e));}};
    const viewId=JSON.stringify([fileChannel,cellId,file.name,document?.instanceId??'readonly',versionId??'current',fullscreen?'fullscreen':'editor',renderer?.id??'pending',renderer?.contentHash??'',diffBaseContent!==undefined]);
    const IframeRenderer=fullscreen?StandaloneSandboxIframe:SandboxIframe;
    const iframeRenderer=content===null||!renderer?null:<IframeRenderer fileHash={file.body_hash??''} documentId={viewId} fileContent={content} documentSnapshot={rendererSnapshot} onSnapshotChange={snapshotChange} onGetContent={getContent} onContentChange={change} diffContent={diffBaseContent} fileName={file.name} rendererId={renderer.id} rendererHash={renderer.contentHash} isActiveFocus={isActive??false} diffHash={diffBaseContent===undefined?undefined:'diff'} diffLabels={diffLabels} cellId={cellId} versionId={versionId} channel={fileChannel} readOnly={requestedReadOnly||diffBaseContent!==undefined||!!snapshot.structuralPending}/>;
    const failure=error??readError??rendererError;
    // Saving is silent: no status row, no "Saved" receipts. The one visible
    // sign is an overlay banner, only once edits have gone SAVE_STALL_MS
    // without reaching the server (no savedSeq progress, or a failed save).
    // Conflicts have their own strip and never count as stalled.
    const stalled=useStalledSave(editable&&!snapshot.competing&&!snapshot.conflicts.length&&(snapshot.editSeq>snapshot.savedSeq||snapshot.status==='Local save failed'||snapshot.status==='Server save failed'||!!saveError),snapshot.savedSeq,flushPendingSave);
    const stallError=saveError||snapshot.error;
    return <div data-file-path={file.name} className={cn('flex flex-col bg-sheet overflow-hidden absolute inset-0 z-50 shadow-(--shadow) group/editor',file.name.endsWith('.excalidraw')?'border-0':'border border-line')}>
        {validationVerdict && !validationVerdict.valid && <div
            data-testid="validation-banner"
            role="alert"
            className="flex-none border-b border-red/25 bg-red/10 px-3 py-2 text-[0.78125rem] text-red"
        >
            Validation failed{validationVerdict.error && `: ${validationVerdict.error}`}
        </div>}
        {!readOnly&&!versionId&&cellId&&file.encoding==='binary'&&<BinaryConflictReview channel={fileChannel} cellId={cellId} file={file}/>}
        {editable && (snapshot.competing||snapshot.conflicts.length>0) && <div className="p-2 text-[0.8125rem] border-b border-line">
            <p>Another version is available. Your edits are preserved.</p>
            <p>Simultaneous editing in separate windows is not supported as collaborative editing. Each window preserves its own edits for explicit resolution.</p>
            <button className={btn} onClick={()=>void (async()=>{
                if(!document)return;
                await document.materializedBody();
                const current=document.getSnapshot();
                const server=await document.competingBody();
                const candidates=await Promise.all(current.conflicts.map(async conflict=>({label:`Preserved version · writer ${conflict.writer_id} · revision ${conflict.candidate_revision} · edit ${conflict.edit_seq}`,body:await document.candidateBody(conflict)})));
                setReview({local:current.body??'',seq:current.editSeq,revision:current.competing?.file_revision??current.baseRevision,server,candidates});setMerged(current.body??'');setLeftVersion(0);setRightVersion(1);
            })().catch(e=>setSaveError(String(e?.message??e)))}>Review versions</button>
            {review && (()=>{
                const versions=[{label:`Your current edits · writer ${document?.writerId} · edit ${review.seq}`,body:review.local},{label:`Saved on server · writer ${snapshot.competing?.writer_id??file.writer_id} · revision ${review.revision}`,body:review.server},...review.candidates];
                const rows=diffLines(versions[leftVersion].body,versions[rightVersion].body);
                const stale=review.seq!==snapshot.editSeq||review.revision!==(snapshot.competing?.file_revision??snapshot.baseRevision);
                return <div role="dialog" aria-label="Review versions">
                    {stale&&<p role="alert">Versions changed. Refresh Review versions before resolving.</p>}
                    <div className="flex gap-2">{[leftVersion,rightVersion].map((index,side)=><label key={side} className="flex-1"><select aria-label={side===0?'Left version':'Right version'} value={index} onChange={e=>(side===0?setLeftVersion:setRightVersion)(Number(e.target.value))}>{versions.map((version,i)=><option key={i} value={i}>{version.label}</option>)}</select><pre aria-label={versions[index].label} className="overflow-auto max-h-60 whitespace-pre-wrap">{rows.map((row,i)=><div key={i} className={row.kind==='add'?'bg-green/10':row.kind==='del'?'bg-red/10':''}>{side===0?row.left:row.right}{'\n'}</div>)}</pre></label>)}</div>
                    <button disabled={stale} className={btn} onClick={()=>void resolve(review.local)}>Keep my edits</button>{' '}
                    <button disabled={stale} className={btn} onClick={()=>void resolve(review.server)}>Use server version</button>{' '}
                    <button className={btn} onClick={()=>setCompeting(review.server)}>Edit merged result</button>{' '}
                    <button className={btn} onClick={()=>{setReview(null);setCompeting(null);}}>Close</button>
                    {competing!==null&&<label>Merged result<textarea value={merged} onChange={e=>setMerged(e.target.value)} className="w-full"/><button disabled={stale} className={btn} onClick={()=>void resolve(merged)}>Save resolution</button></label>}
                </div>;
            })()}
        </div>}
        {snapshot.recoveries.map(record=><div key={record.writerId} className="p-2 text-[0.8125rem]">A draft from another window is available. <button className={btn} onClick={()=>void document?.recover(record).catch(e=>setSaveError(String(e?.message??e)))}>Recover its text</button></div>)}
        <div className="flex-1 relative overflow-hidden bg-sheet pointer-events-auto">
            {stalled && <div role="alert" className={cn(dialog,'absolute top-2 inset-x-2 z-10 flex flex-wrap items-center gap-2 px-3 py-2 text-[0.78125rem] text-ink')}>
                <span>{stallError?'Still trying to save your edits.':'Still saving your edits.'}</span>
                {stallError && <span className="text-ink-2 min-w-0 truncate">{stallError}</span>}
                <button className={cn(btn,'ml-auto')} onClick={()=>void flushPendingSave().catch(()=>undefined)}>Retry now</button>
            </div>}
            {failure?<div role="alert" className="p-3">Could not load editor: {failure}{rendererError&&<button className={btn} onClick={()=>void refreshRendererCache()}>Retry renderer</button>}</div>:!renderer?<div className="p-3">Loading renderer…</div>:content===null?<div className="p-3">Loading file content…</div>:renderer.id.startsWith('definition-') && diffBaseContent===undefined?<React.Suspense fallback={<p>Loading definition editor…</p>}><DefinitionViewer versionId={versionId} path={file.name} content={content} files={files} cellId={cellId} channel={fileChannel} readOnly={readOnly||!!snapshot.structuralPending} onSave={change} onUpdateFiles={onUpdateFiles} onOpenFile={onOpenFile}/></React.Suspense>:renderer.id==='binary'?<div className="p-3">Cannot display filetype</div>:diffBaseContent!==undefined && !renderer.supportsDiff?<div className="p-3">filetype doesn't support diff</div>:renderer.embed==='native'?<NativeRendererErrorBoundary resetKey={`${renderer.id}::${renderer.contentHash}::${file.name}`} fallback={iframeRenderer}><NativeRenderer rendererId={renderer.id} rendererHash={renderer.contentHash} fileContent={content} documentSnapshot={rendererSnapshot} onSnapshotChange={snapshotChange} onGetContent={getContent} onContentChange={change} fileName={file.name} cellId={cellId} versionId={versionId} channel={fileChannel} isActiveFocus={isActive??false} isDiffMode={diffBaseContent!==undefined} diffContent={diffBaseContent} diffLabels={diffLabels} visible={visible} readOnly={requestedReadOnly||diffBaseContent!==undefined||!!snapshot.structuralPending}/></NativeRendererErrorBoundary>:iframeRenderer}
        </div>
    </div>;
});
