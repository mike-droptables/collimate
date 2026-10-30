import {retainedFileMetadata} from '@/lib/fileDocuments';
import {currentChannelId} from '@/lib/currentChannel';
import {textFile} from '@/lib/fileCommands';
// The body of one content-pane tab. A pure dispatch on tab.kind — every
// branch is a surface that already existed; this file only decides which one
// mounts and hands it the per-tab focus/visibility flags derived by
// ContentPane (see DESIGN §3.5, rules R9/R10/R11/R14/R20).
//
// This component is mounted ONCE per has-been-active tab and then kept
// mounted (ContentPane hides inactive tabs with display:none, never
// visibility/opacity — pooled iframes park via IntersectionObserver and
// native guest views composite above all DOM, so a merely-transparent tab
// would paint over the foreground one).

import React from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Cell, VirtualFile } from '@/lib/types';
import type { UpdateFiles } from '@/lib/fileTransfer';
import type { HistoryNode } from '@/lib/api';
import type { CellTab } from '@/stores/collimate/types';
import type { CellValidation } from '@/lib/definitionApi';
import { FullFileEditor, type FullFileEditorHandle } from '@/components/FullFileEditor';
import { CompactPinnedPreview } from './CompactPinnedPreview';
import { EmptyTab } from './EmptyTab';
import { CompareTabBody } from './CompareTabBody';
import { TabNotice } from './TabNotice';

export interface TabBodyProps {
    tab: CellTab;
    /** Current server check for this file, absent while edits or a refresh make it stale. */
    validationVerdict?: CellValidation['verdicts'][string];
    cell: Cell;
    channel?: string;
    /** R9: cell active ∧ this leaf is the active leaf ∧ this tab is its leaf's
     *  active tab ∧ nothing else is maximized. Gates renderer interactivity. */
    isActiveFocus: boolean;
    /** R10: the renderer is actually painting (cell on screen and this tab is
     *  its leaf's active tab). An open cell panel does not clear it: the panel
     *  conceals the pooled iframes through `CellPanelConceal` instead. */
    visible: boolean;
    readOnly: boolean;
    /** R11: only the focused tab of the focused cell answers Esc/Cmd+S. */
    editorShortcutsEnabled: boolean;
    diffBaseFiles?: Record<string, string>;
    /** Before/after labels for diffBaseFiles (the base and current cell names). */
    diffLabels?: { base: string; current: string };
    registerEditorHandle: (tabId: string, handle: FullFileEditorHandle | null) => void;
    /** CellEditor closes the file's tab (and clears mainFocusMode). */
    onCloseFileTab: (path: string) => void;
    onUpdateFiles: UpdateFiles;
    /** R20: during a zoom transition the pinned file shows the cheap stand-in
     *  instead of booting a renderer. */
    isZoomTransitioning: boolean;
    /** R13: this body is the maximized tab, mounted in CellEditor's body-level
     *  overlay. Swaps the pooled in-canvas iframe for the standalone one (the
     *  overlay would otherwise occlude the pooled one, which lives inside the
     *  canvas transform) and always shows the real renderer. */
    fullscreen?: boolean;
    /** Explicit save (Cmd+S) drain — wired to CellEditor's saveNow. */
    onAfterSave?: () => void;
    /** Id-keyed close for non-file tabs (a history or compare tab's Esc/✕
     *  must never route through the path-keyed onCloseFileTab — it would
     *  close the LIVE tab of the same path). */
    onCloseTabById: (tabId: string) => void;
    /** History-mode view data. History tabs resolve their content from the
     *  CURRENT scrub selection at render time (tabs follow the scrub). */
    historyView?: {
        node: HistoryNode | null;
        /** The selected version's cell id — the non-live cellId history file
         *  tabs mount under, so every cellId-keyed renderer channel (events,
         *  sibling fetches, draft writes) resolves against the version, never
         *  the live cell. */
        versionCellId: string | null;
        files: VirtualFile[] | null;
        baseFiles: Record<string, string> | null;
        artifactText: (blobHash: string) => string | undefined;
    } | null;
}

const TabBodyContent: React.FC<TabBodyProps> = ({
    tab, validationVerdict, cell, channel, isActiveFocus, visible, readOnly,
    editorShortcutsEnabled, diffBaseFiles, diffLabels, registerEditorHandle, onCloseFileTab,
    onUpdateFiles, isZoomTransitioning, fullscreen = false,
    onAfterSave, onCloseTabById, historyView,
}) => {
    const cellId = cell.id;

    if (tab.kind === 'history') {
        const hv = historyView;
        if (!hv || !hv.node) return <TabNotice title="Loading history…" />;
        const close = () => onCloseTabById(tab.id);
        if (tab.category === 'file') {
            if (hv.files === null) return <TabNotice title="Loading version files…" />;
            const file = hv.files.find(f => f.name === tab.path);
            if (!file) {
                return <TabNotice title={`${tab.path} — removed in this node`}
                    sub="Scrub to a node that contains it, or close this tab" />;
            }
            const base = hv.baseFiles ? hv.baseFiles[file.name] : undefined;
            return (
                <FullFileEditor
                    ref={(handle) => { registerEditorHandle(tab.id, handle); }}
                    file={file}
                    diffBaseContent={base}
                    diffLabels={base !== undefined ? { base: 'previous node', current: 'this node' } : undefined}
                    onClose={close}
                    readOnly
                    isActive={isActiveFocus}
                    cellId={hv.versionCellId ?? `hist:${hv.node.node_id}`}
                    versionId={hv.versionCellId ?? undefined}
                    channel={channel}
                    fullscreen={fullscreen}
                    editorShortcutsEnabled={editorShortcutsEnabled}
                    visible={visible}
                    rendererId={tab.rendererId}
                />
            );
        }
        const listing = tab.category === 'config' ? hv.node.config : hv.node.logs;
        const artifact = listing.find(a => a.path === tab.path);
        if (!artifact) {
            return <TabNotice title={`${tab.path} — not captured for this node`}
                sub={hv.node.kind === 'run' ? undefined : 'File-edit nodes carry no config or logs'} />;
        }
        const text = hv.artifactText(artifact.blob_hash);
        if (text === undefined) return <TabNotice title="Loading…" />;
        const ext = tab.path.split('.').pop() ?? '';
        const lang = ({ yaml: 'yaml', yml: 'yaml', set_yaml: 'yaml', json: 'json', md: 'markdown', txt: 'plaintext' } as Record<string, string>)[ext] ?? 'plaintext';
        const artifactFile=textFile(tab.path,text,lang);
        return (
            <FullFileEditor
                ref={(handle) => { registerEditorHandle(tab.id, handle); }}
                file={artifactFile}
                onClose={close}
                readOnly
                isActive={isActiveFocus}
                cellId={`hist:${hv.node.node_id}`}
                channel={channel}
                fullscreen={fullscreen}
                editorShortcutsEnabled={editorShortcutsEnabled}
                visible={visible}
                rendererId={tab.rendererId}
            />
        );
    }

    if (tab.kind === 'compare') {
        // The current side is this cell's live file; reconcile drops the tab
        // once it is gone, so render nothing for the frame in between.
        const file = cell.files.find(f => f.name === tab.current.fileName);
        if (!file) return null;
        return (
            <CompareTabBody
                tab={tab}
                file={file}
                isActiveFocus={isActiveFocus}
                visible={visible}
                fullscreen={fullscreen}
                editorShortcutsEnabled={editorShortcutsEnabled}
                onCloseTabById={onCloseTabById}
            />
        );
    }

    if (tab.kind === 'file') {
        const file = cell.files.find(f => f.name === tab.path)??retainedFileMetadata(channel||currentChannelId(),cell.id).find(f=>f.name===tab.path);
        // reconcile() drops tabs for vanished files on the next pass; render
        // nothing for the frame in between rather than throwing.
        if (!file) return null;
        // The maximized mount always wants the real renderer — the cheap
        // stand-in exists to keep zoom animations smooth in the canvas.
        if (!fullscreen && isZoomTransitioning && tab.path === cell.pinned) {
            return <CompactPinnedPreview cell={cell} />;
        }
        const diffBase = diffBaseFiles ? diffBaseFiles[file.name] : undefined;
        return (
            <FullFileEditor
                ref={(handle) => { registerEditorHandle(tab.id, handle); }}
                file={file}
                files={cell.files}
                onUpdateFiles={onUpdateFiles}
                onOpenFile={(path) => useCollimateStore.getState().applyReveal(cellId, path, '', true)}
                diffBaseContent={diffBase}
                diffLabels={diffBase !== undefined ? diffLabels : undefined}
                onClose={() => onCloseFileTab(tab.path)}
                readOnly={readOnly}
                isActive={isActiveFocus}
                cellId={cellId}
                channel={channel}
                onAfterSave={readOnly ? undefined : onAfterSave}
                fullscreen={fullscreen}
                editorShortcutsEnabled={editorShortcutsEnabled}
                visible={visible}
                rendererId={tab.rendererId}
                validationVerdict={validationVerdict}
            />
        );
    }

    // A built-in card's files are read-only, yet its Files panel still
    // finds and opens them: only a read-only cell (no panel) disables it.
    return <EmptyTab cellId={cellId} readOnly={readOnly && !cell.builtin} />;
};


/** File tabs observe their own file; sibling edits do not repaint hidden renderers. */
function sameTabBody(prev: TabBodyProps, next: TabBodyProps): boolean {
    const fileTab = prev.tab.kind === 'file' && next.tab.kind === 'file';
    if (!fileTab) return Object.keys(prev).length === Object.keys(next).length
        && (Object.keys(prev) as (keyof TabBodyProps)[]).every(key => Object.is(prev[key], next[key]));
    if (prev.cell.id !== next.cell.id || prev.cell.pinned !== next.cell.pinned) return false;
    if ((prev.isZoomTransitioning || next.isZoomTransitioning) && prev.cell !== next.cell) return false;
    const oldFile = prev.cell.files.find(file => file.name === (prev.tab as Extract<CellTab, {kind: 'file'}>).path);
    const newFile = next.cell.files.find(file => file.name === (next.tab as Extract<CellTab, {kind: 'file'}>).path);
    if (oldFile !== newFile && (!oldFile || !newFile
        || Object.keys(oldFile).length !== Object.keys(newFile).length
        || (Object.keys(oldFile) as (keyof VirtualFile)[]).some(key => !Object.is(oldFile[key], newFile[key])))) return false;
    const ignored = new Set<keyof TabBodyProps>(['cell', 'historyView', 'onUpdateFiles', 'diffBaseFiles']);
    const path = (next.tab as Extract<CellTab, {kind: 'file'}>).path;
    if (prev.diffBaseFiles?.[path] !== next.diffBaseFiles?.[path]) return false;
    return [...new Set([...Object.keys(prev), ...Object.keys(next)])].every(key =>
        ignored.has(key as keyof TabBodyProps) || Object.is(prev[key as keyof TabBodyProps], next[key as keyof TabBodyProps]));
}

export const TabBody = React.memo(TabBodyContent, sameTabBody);
