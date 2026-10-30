// Paste into a cell's Files pane: one owner shared by the tree (FileBrowser)
// and the grid (FolderView). Ctrl+V reaches it as the native `paste` event
// (FileBrowser and FolderView leave the Mod+V keydown to the browser); the
// context menu's "Paste here" reaches it through pasteHere, which reads the async Clipboard API.
//
// The listener sits on window in the capture phase and acts only while the
// keyboard focus is inside this surface and not in an editable: Chromium
// aims a paste at whatever holds the selection, which need not be the
// focused (non-editable) row, so the event's target says nothing reliable.
// What lands: the internal clipboard through useFileTransfer's pasteInto;
// an image, a text or one OS file after a filename prompt (AddFileDialog);
// several OS files under their own names after one overwrite question.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { VirtualFile } from '@/lib/types';
import { AddFileDialog } from '@/components/AddFileDialog';
import {
    classifyClipboardItems, classifyPaste, defaultPasteStem, mergeAgreed, overwriteConfirm,
    overwrittenNames, splitFileName, type PasteContent,
} from '@/lib/clipboardPaste';
import { ingestFilesToVirtualFiles } from '@/lib/fileIngest';
import { FOLDER_KEEP_FILE, isFolderKeep } from '@/lib/fileTree';
import { runPathRefusal } from '@/lib/fileLocks';
import { joinPath, summarize, type UpdateFiles } from '@/lib/fileTransfer';
import { isEditableTarget } from '@/lib/nativeSelection';
import { isRunPath } from '@/lib/runningCell';
import { useCollimateStore } from '@/stores/useCollimateStore';

export interface FilesPanePasteOptions {
    /** The surface's root: a paste acts only while focus is inside it. */
    containerRef: React.RefObject<HTMLElement | null>;
    /** The folder a Ctrl+V lands in, read at paste time. */
    targetFolder: () => string;
    files: VirtualFile[];
    /** Absent = no cell (a read-only embed): nothing is pasted. */
    cellId: string | undefined;
    readOnly: boolean;
    onUpdateFiles?: UpdateFiles;
    onOpenFile: (path: string) => void;
    /** useFileTransfer's paste of the internal clipboard. */
    pasteInto: (folder: string) => void;
}

/** A pasted image, text or single file waiting on its filename. */
interface PendingPaste {
    folder: string;
    body: Blob;
    title: string;
    stem: string;
    ext: string;
    x: number;
    y: number;
}

export const PASTE_FROM_KEYBOARD_HINT = 'Press Ctrl+V in the files pane to paste from the system clipboard';

/** Where a new file at `path` cannot go: a file of that name, or a folder. */
function pathTaken(path: string, files: readonly VirtualFile[]): boolean {
    return files.some(f => f.name === path || f.name.startsWith(`${path}/`));
}

export function useFilesPanePaste({
    containerRef, targetFolder, files, cellId, readOnly, onUpdateFiles, onOpenFile, pasteInto,
}: FilesPanePasteOptions) {
    const writable = !!cellId && !readOnly && !!onUpdateFiles;
    const [pending, setPending] = useState<PendingPaste | null>(null);
    // The listener and the async writes read the latest props, not the
    // render that registered them.
    const latest = useRef({ targetFolder, files, onUpdateFiles, onOpenFile, pasteInto });
    useEffect(() => { latest.current = { targetFolder, files, onUpdateFiles, onOpenFile, pasteInto }; });

    /** The prompt's corner: under the focused row, else the surface's top left. */
    const anchor = useCallback((): { x: number; y: number } => {
        const container = containerRef.current;
        const active = document.activeElement;
        if (container && active instanceof HTMLElement && active !== container && container.contains(active)) {
            const r = active.getBoundingClientRect();
            return { x: r.left, y: r.bottom + 4 };
        }
        const r = container?.getBoundingClientRect();
        return { x: (r?.left ?? 0) + 8, y: (r?.top ?? 0) + 8 };
    }, [containerRef]);

    /** Several OS files under their own names: one question covers every
     *  file they would replace. */
    const pasteFiles = useCallback(async (picked: File[], folder: string) => {
        const write = latest.current.onUpdateFiles;
        if (!write) return;
        const store = useCollimateStore.getState();
        const entries = picked.map(file => ({ path: joinPath(folder, file.name), file }));
        const reserved = entries.filter(e => isRunPath(e.path) || isFolderKeep(e.path)).map(e => e.path);
        const blocked = entries.filter(e => !reserved.includes(e.path)
            && latest.current.files.some(f => f.name.startsWith(`${e.path}/`))).map(e => e.path);
        const landing = entries.filter(e => !reserved.includes(e.path) && !blocked.includes(e.path));
        const replacing = overwrittenNames(landing.map(e => ({ name: e.path })), latest.current.files);
        if (replacing.length > 0 && !await store.openConfirm({ cellId, ...overwriteConfirm(replacing, folder ? `\`${folder}\`` : 'the cell') })) return;
        const ingested = await ingestFilesToVirtualFiles(landing);
        let kept: string[] = [];
        if (ingested.length > 0) {
            await write(current => {
                const merged = mergeAgreed(current, ingested, new Set(replacing));
                kept = merged.kept;
                return merged.files;
            });
        }
        const taken = [...blocked, ...kept];
        const skipped = reserved.length + taken.length;
        if (skipped > 0) {
            const reasons = [
                taken.length > 0 ? `Something of the same name is already in the cell: ${summarize(taken)}` : '',
                reserved.length > 0 ? `These names are reserved for runs or folder markers: ${summarize(reserved)}` : '',
            ];
            void store.openAlert({ cellId,
                title: skipped === 1 ? 'One file was not pasted' : `${skipped} files were not pasted`,
                body: reasons.filter(Boolean).join('\n\n'),
            });
        }
    }, []);

    const dispatch = useCallback((content: PasteContent, folder: string) => {
        if (content.kind === 'internal') { latest.current.pasteInto(folder); return; }
        if (content.kind === 'files' && content.files.length > 1) {
            void pasteFiles(content.files, folder).catch(error => useCollimateStore.getState()
                .openAlert({ cellId, title: 'Paste failed', body: String((error as Error)?.message ?? error) }));
            return;
        }
        const now = new Date();
        const at = anchor();
        if (content.kind === 'files') {
            const [file] = content.files;
            const { stem, ext } = splitFileName(file.name);
            setPending({ folder, body: file, title: 'Paste file', stem, ext, ...at });
        } else if (content.kind === 'image') {
            setPending({ folder, body: content.blob, title: 'Paste image', stem: defaultPasteStem('image', now), ext: content.ext, ...at });
        } else {
            setPending({
                folder, body: new Blob([content.text], { type: 'text/plain' }), title: 'Paste text',
                stem: defaultPasteStem('text', now), ext: content.ext, ...at,
            });
        }
    }, [anchor, pasteFiles]);

    useEffect(() => {
        if (!writable) return;
        const onPaste = (e: ClipboardEvent) => {
            const container = containerRef.current;
            const active = document.activeElement;
            if (!container || !active || !container.contains(active) || isEditableTarget(active)) return;
            const folder = latest.current.targetFolder();
            if (runPathRefusal(folder) || !e.clipboardData) return;
            const { fileClipboard, copiedOsText } = useCollimateStore.getState();
            const content = classifyPaste(e.clipboardData, fileClipboard, copiedOsText);
            if (!content) return;
            e.preventDefault();
            dispatch(content, folder);
        };
        window.addEventListener('paste', onPaste, true);
        return () => window.removeEventListener('paste', onPaste, true);
    }, [writable, containerRef, dispatch]);

    /** The context menu's Paste: the internal clipboard first, else what
     *  the async Clipboard API hands over (an image or text, never OS
     *  files). Where the browser refuses, Ctrl+V is the way. */
    const pasteHere = useCallback(async (folder: string) => {
        if (!writable || runPathRefusal(folder)) return;
        const store = useCollimateStore.getState();
        if (store.fileClipboard) { latest.current.pasteInto(folder); return; }
        let content: PasteContent | null;
        try {
            content = await classifyClipboardItems(await navigator.clipboard.read(), null, store.copiedOsText);
        } catch {
            void store.openAlert({ cellId, title: 'Paste here', body: PASTE_FROM_KEYBOARD_HINT });
            return;
        }
        if (!content) {
            void store.openAlert({ cellId, title: 'Nothing to paste', body: 'The clipboard holds no image or text.' });
            return;
        }
        dispatch(content, folder);
    }, [writable, dispatch]);

    /** The prompt's Create: the named file streams into the CAS, then lands
     *  if its path is still free in the cell as it is at commit time. */
    const commitPending = useCallback(async (name: string) => {
        const write = latest.current.onUpdateFiles;
        if (!pending || !write) return;
        const path = joinPath(pending.folder, name);
        // The rules New file applies (useFileOperations.handleCreateFileAtPath).
        if (name === FOLDER_KEEP_FILE) return { ok: false as const, error: 'Reserved name' };
        const refusal = runPathRefusal(path);
        if (refusal) return { ok: false as const, error: refusal };
        const taken = { ok: false as const, error: `\`${path}\` already exists` };
        if (pathTaken(path, latest.current.files)) return taken;
        const [stored] = await ingestFilesToVirtualFiles([{ path, file: new File([pending.body], name, { type: pending.body.type }) }]);
        if (!stored) return { ok: false as const, error: 'The pasted content could not be stored.' };
        let clash = false;
        await write(current => {
            if (pathTaken(path, current)) { clash = true; return current; }
            return [...current, stored];
        });
        if (clash) return taken;
        setPending(null);
        latest.current.onOpenFile(path);
        return { ok: true as const };
    }, [pending]);

    const prompt = pending && React.createElement(AddFileDialog, {
        x: pending.x,
        y: pending.y,
        basePath: pending.folder,
        existingNames: files.map(f => f.name),
        mode: 'file',
        title: pending.title,
        initialStem: pending.stem,
        initialExt: pending.ext,
        onCancel: () => setPending(null),
        onCreate: commitPending,
    });

    return { pasteHere, prompt };
}
