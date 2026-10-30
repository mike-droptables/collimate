import { useCallback } from 'react';
import type { VirtualFile } from '@/lib/types';
import { TreeNode, FOLDER_KEEP_FILE, isFolderKeep, remintEmptiedParent } from '@/lib/fileTree';
import {
    collectTransferEntries, fetchTextBodies, rebaseEntries, mergeByName, parentOf, withFetchedBodies,
    type UpdateFiles, type WithBody,
} from '@/lib/fileTransfer';
import { ingestFilesToVirtualFiles, folderMarkersFor } from '@/lib/fileIngest';
import { collectDroppedFiles, type DropCapture } from '@/lib/dropEntries';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { isRunPath } from '@/lib/runningCell';
import { folderOutsideRuns, lockOwnerUnder, runPathRefusal, type LockOwnerOf } from '@/lib/fileLocks';
import { languageForPath } from '@/lib/fileLanguage';
import { needsTextBody, textFile } from '@/lib/fileCommands';
import { flushCellSaves } from '@/lib/cellSaveBarrier';
import {flushRendererEditors} from '@/lib/rendererFlush';
import {getFileDocument} from '@/lib/fileDocuments';
import type { FileTransferEntry } from '@/stores/collimate/types';

interface UseFileOperationsParams {
    /** The cell's files as this render sees them: validation, prompts and
     *  body fetches read them. Every write is an updater `onUpdateFiles`
     *  applies to the cell's latest list (lib/fileTransfer UpdateFiles). */
    files: VirtualFile[];
    onUpdateFiles?: UpdateFiles;
    readOnly: boolean;
    /** The live run owning a path (lib/fileLocks). A locked file — and a
     *  folder holding one — is never deleted or renamed; nothing new is
     *  created or dropped under `_runs/`. Absent = nothing is locked. */
    lockOwnerOf?: LockOwnerOf;
    /** File operations use pinned bodies and conditional durable receipts. */
    channel?: string;
    cellId?: string;
}

export type CreationResult={ok:true;path:string}|{ok:false;error:string};
export function useFileOperations({ files, onUpdateFiles, readOnly, lockOwnerOf, channel, cellId }: UseFileOperationsParams) {
    const isLockedNode = useCallback((node: TreeNode) => !!lockOwnerUnder(
        files.map(f => f.name), node.path, node.type === 'folder', lockOwnerOf,
    ), [files, lockOwnerOf]);

    /** File operations use pinned bodies and conditional durable receipts. */
    const withBodies = useCallback(async (
        names: string[],
        failTitle: string,
        write: (withBody: WithBody, refuse: () => void) => void | Promise<void>,
    ): Promise<void> => {
        const refuse = () => {
            void useCollimateStore.getState().openAlert({ cellId,
                title: failTitle,
                body: 'Could not read the requested file revision. Try again.',
            });
        };
        const wanted = new Set(names);
        const descriptors = files.filter(f => wanted.has(f.name) && needsTextBody(f));
        let bodies = new Map<string, string>();
        if (descriptors.length > 0) {
            if (!channel || !cellId) return refuse();
            try {
                bodies = await fetchTextBodies(channel, cellId, descriptors, { cache: 'no-store' });
            } catch {
                return refuse();
            }
        }
        if (cellId) await flushCellSaves(cellId);
        await write(withFetchedBodies(descriptors, bodies), refuse);
    }, [files, channel, cellId]);

    /** Create a file at the user-typed path. Accepts bare filenames
     *  (`foo.md`) or paths (`src/components/Bar.tsx`); folders emerge
     *  from path prefixes — no separate "add folder" action.
     *
     *  `basePath` is the folder the popup was anchored to (empty for
     *  the root "+"). If the user's input already contains a `/`, we
     *  treat it as an absolute-within-cell path and ignore basePath;
     *  otherwise basePath is prepended.
     *
     *  Returns `{ ok: true, path }` on success and `{ ok: false, error }`
     *  for validation failures. The caller owns the popup UI. */
    const handleCreateFileAtPath = useCallback(async (
        rawInput: string,
        basePath: string = '',
    ): Promise<CreationResult> => {
        if (!onUpdateFiles || readOnly) return { ok: false, error: 'Read-only' };
        const normalized = rawInput.replace(/\\/g, '/').trim();
        if (normalized.length === 0) return { ok: false, error: 'Enter a filename' };
        if (normalized.startsWith('/')) return { ok: false, error: "Paths can't start with /" };
        // If the input has no slash, treat it as relative to basePath.
        // Inputs with a slash stand on their own (ignore basePath) so
        // the user can always escape a folder context by typing a full
        // path.
        const path = normalized.includes('/')
            ? normalized
            : basePath
                ? `${basePath}/${normalized}`
                : normalized;
        // Final sanity: no empty segments ("a//b"), no trailing slash.
        if (path.endsWith('/')) return { ok: false, error: 'Path must include a filename' };
        const segments = path.split('/');
        if (segments.some(seg => seg.length === 0)) return { ok: false, error: 'Empty path segment' };
        if (segments.some(seg => seg === '.' || seg === '..')) return { ok: false, error: 'Invalid path segment' };
        if (segments[segments.length - 1] === FOLDER_KEEP_FILE) return { ok: false, error: 'Reserved name' };
        const refusal = runPathRefusal(path);
        if (refusal) return { ok: false, error: refusal };
        if (files.some(f => f.name === path)) return { ok: false, error: `\`${path}\` already exists` };
        // A file and a folder can't share a path, and a file can't hold one.
        if (files.some(f => f.name.startsWith(path + '/'))) return { ok: false, error: `A folder named \`${path}\` already exists` };
        for (let i = 1; i < segments.length; i++) {
            const prefix = segments.slice(0, i).join('/');
            if (files.some(f => f.name === prefix)) return { ok: false, error: `\`${prefix}\` is a file` };
        }
        const newFile=textFile(path,'',languageForPath(path));
        try{await onUpdateFiles(current => (current.some(f => f.name === path) ? current : [...current, newFile]));}
        catch(error){return {ok:false,error:String((error as Error)?.message??error)};}
        return { ok: true, path };
    }, [files, onUpdateFiles, readOnly]);

    /** Create a folder in `basePath` ('' = root). Folders persist via a hidden
     *  `<folder>/.collkeep` marker. `name` is a single segment (no slashes). */
    const handleCreateFolder = useCallback(async (
        basePath: string,
        rawName: string,
    ): Promise<CreationResult> => {
        if (!onUpdateFiles || readOnly) return { ok: false, error: 'Read-only' };
        const name = rawName.replace(/\\/g, '/').trim();
        if (name.length === 0) return { ok: false, error: 'Enter a folder name' };
        if (name.includes('/')) return { ok: false, error: "Folder name can't contain /" };
        if (name === '.' || name === '..') return { ok: false, error: 'Invalid folder name' };
        if (name === FOLDER_KEEP_FILE) return { ok: false, error: 'Reserved name' };
        const folderPath = basePath ? `${basePath}/${name}` : name;
        const refusal = runPathRefusal(folderPath);
        if (refusal) return { ok: false, error: refusal };
        const prefix = folderPath + '/';
        if (files.some(f => f.name === folderPath)) return { ok: false, error: `A file named \`${name}\` already exists` };
        if (files.some(f => f.name.startsWith(prefix))) return { ok: false, error: `\`${folderPath}\` already exists` };
        const marker=textFile(`${folderPath}/${FOLDER_KEEP_FILE}`,'');
        try{await onUpdateFiles(current => (current.some(f => f.name.startsWith(prefix)) ? current : [...current, marker]));}
        catch(error){return {ok:false,error:String((error as Error)?.message??error)};}
        return { ok: true, path: folderPath };
    }, [files, onUpdateFiles, readOnly]);

    /** Duplicate a file or folder in place (same parent) with a `(copy)`
     *  suffix, reusing the cross-cell rebase engine for conflict-renaming.
     *  A run's file is duplicated OUTSIDE `_runs/` (the copy is an ordinary
     *  work file; only a run writes its folder). */
    const handleDuplicate = useCallback((node: TreeNode): Promise<void> | void => {
        if (!onUpdateFiles || readOnly) return;
        const isFolder = node.type === 'folder';
        const entries = collectTransferEntries(files, node.path, isFolder);
        if (entries.length === 0) return;
        return withBodies(entries.map(e => e.file.name), 'Duplicate failed', (withBody, refuse) => onUpdateFiles(current => {
            // Match fetched bytes only to their unchanged revision/hash.
            const fresh: FileTransferEntry[] = [];
            for (const e of collectTransferEntries(current, node.path, isFolder)) {
                const file = withBody(e.file);
                if (!file) {
                    refuse();
                    return current;
                }
                fresh.push({ ...e, file });
            }
            if (fresh.length === 0) return current;
            const target = folderOutsideRuns(parentOf(node.path));
            const { files: additions } = rebaseEntries(fresh, target, new Set(current.map(f => f.name)));
            return mergeByName(current, additions);
        }));
    }, [files, onUpdateFiles, readOnly, withBodies]);

    const handleRename = useCallback(async (node: TreeNode, rawName: string): Promise<void> => {
        if (!onUpdateFiles || readOnly) return;
        const newName = rawName.trim();
        if (node.name === newName) return;
        if (isRunPath(node.path)||isLockedNode(node)) return;
        const newPath = [...node.path.split('/').slice(0, -1), newName].join('/');
        if (isRunPath(newPath)) return;
        const refuse = (body: string) => { void useCollimateStore.getState().openAlert({ cellId, title: 'Rename refused', body }); };
        // Same rules as a new folder: one segment, not reserved.
        if (newName.length === 0) return refuse('Enter a name');
        if (newName.includes('/') || newName.includes('\\')) return refuse("A name can't contain /");
        if (newName === '.' || newName === '..') return refuse('Invalid name');
        if (newName === FOLDER_KEEP_FILE) return refuse('Reserved name');
        // Two entries at one path would lose one of them; a folder renamed
        // onto another would merge the two.
        const taken = (list: VirtualFile[]) => list.some(f => f.name === newPath || f.name.startsWith(newPath + '/'));
        if (taken(files)) return refuse(`\`${newPath}\` already exists`);

        const oldPrefix = node.path + '/';
        const newPrefix = newPath + '/';
        const moved = node.type === 'folder'
            ? files.filter(f => f.name.startsWith(oldPrefix)).map(f => f.name)
            : [node.path];
        const owners=channel&&cellId?moved.flatMap(path=>{const doc=getFileDocument(channel,cellId,path);return doc?[doc]:[];}):[];
        if(owners.some(doc=>doc.getSnapshot().competing||doc.getSnapshot().conflicts.length))return refuse('Review versions before renaming this file.');
        if(owners.some(doc=>doc.getSnapshot().editSeq>doc.getSnapshot().savedSeq)){
            const confirmed=await useCollimateStore.getState().openConfirm({ cellId,title:'Save your edits and rename?',body:`Rename to ${newPath}. Your current edits will be saved at the new path together with the source deletion.`,confirmLabel:'Save and rename',cancelLabel:'Cancel'});
            if(!confirmed)return;
        }
        if(cellId)await flushRendererEditors(cellId);
        const renames:import('@/lib/fileTransfer').FileRename[]=[];
        return onUpdateFiles(current => {
            if (lockOwnerUnder(current.map(f => f.name), node.path, node.type === 'folder', lockOwnerOf)) throw new Error('Rename source is read-only.');
            if (taken(current)) throw new Error(`\`${newPath}\` already exists`);
            if (node.type === 'file') {
                const idx = current.findIndex(f => f.name === node.path);
                if (idx < 0) throw new Error('Rename source is no longer available.');
                const f = current[idx];
                const newFiles = [...current];
                renames.push({source:f.name,target:newPath});
                // A binary file stays 'binary'; a text file takes its new extension's language.
                newFiles[idx] = { ...f, name: newPath, language: f.encoding === 'binary' ? f.language : languageForPath(newPath) };
                return newFiles;
            }
            const renamed: VirtualFile[] = [];
            for (const f of current) {
                if (!f.name.startsWith(oldPrefix)) {
                    renamed.push(f);
                    continue;
                }
                const name=newPrefix+f.name.slice(oldPrefix.length);
                renames.push({source:f.name,target:name});
                renamed.push({...f,name});
            }
            if(!renames.length)throw new Error('Rename source is no longer available.');
            return renamed;
        },{renames});
    }, [files, onUpdateFiles, readOnly, isLockedNode, lockOwnerOf, channel,cellId]);

    const handleDelete = useCallback(async (node: TreeNode) => {
        if (!onUpdateFiles || readOnly || isRunPath(node.path) || isLockedNode(node)) return;
        // What the dialog asks about, and all the delete removes: a file, or
        // a folder's whole subtree (its .collkeep markers included).
        const folderPrefix = node.path + '/';
        const doomed = node.type === 'folder'
            ? files.filter(f => f.name.startsWith(folderPrefix)).map(f => f.name)
            : [node.path];
        // Folders delete recursively — surface the file count so the
        // user can't accidentally wipe a big subtree. Files just show
        // their name.
        let body: string;
        if (node.type === 'folder') {
            // Don't count the hidden .collkeep marker(s) toward the user-facing total.
            const count = doomed.filter(name => !isFolderKeep(name)).length;
            // A folder with no real files but a deeper (subfolder) marker isn't
            // "empty" — it holds empty subfolders. Exclude its own direct marker.
            const ownMarker = `${folderPrefix}${FOLDER_KEEP_FILE}`;
            const hasSubfolders = doomed.some(name => name !== ownMarker);
            body = count === 0
                ? (hasSubfolders
                    ? `Delete folder \`${node.path}\` and its subfolders?`
                    : `Delete empty folder \`${node.path}\`?`)
                : count === 1
                    ? `Delete \`${node.path}\`? 1 file will be removed.`
                    : `Delete \`${node.path}\`? ${count} files will be removed.`;
        } else {
            body = `Delete \`${node.name}\`?`;
        }
        const ok = await useCollimateStore.getState().openConfirm({ cellId,
            title: node.type === 'folder' ? 'Delete folder' : 'Delete file',
            body,
            confirmLabel: 'Delete',
            destructive: true,
        });
        if (!ok) return;

        // Applied to the list as it is after the dialog, not the one it
        // opened over: whatever landed while it was up stays, inside the
        // folder too, since the dialog never asked about it.
        const removing = new Set(doomed);
        try{await onUpdateFiles(current => {
            const remaining = current.filter(f => !removing.has(f.name));
            if (remaining.length === current.length) return current;
            // Re-mint the parent's .collkeep if this emptied it, so a
            // user-created folder doesn't vanish just because it was emptied.
            return remintEmptiedParent(remaining, node.path);
        });}catch(error){await useCollimateStore.getState().openAlert({ cellId,title:'Delete failed',body:String((error as Error)?.message??error)});}
    }, [files, onUpdateFiles, readOnly, isLockedNode]);

    /** OS drop into this cell. Takes the SYNCHRONOUS capture the drop handler
     *  made (see lib/dropEntries) rather than a FileList: a dropped folder is
     *  only visible through the entries API, and those items die with the drop
     *  event. The capture is then walked here, off the event, so the whole
     *  recursive tree lands — paths and empty folders included. */
    const handleDropFiles = useCallback(async (capture: DropCapture, targetPath: string) => {
        if (!onUpdateFiles || readOnly) return;
        const refusal = runPathRefusal(targetPath);
        if (refusal) {
            void useCollimateStore.getState().openAlert({ cellId, title: 'Drop refused', body: refusal });
            return;
        }

        try {
            // Walk the drop (folders → their files, at their relative paths),
            // then shared ingest (binary → CAS blob_hash, text → inline), then
            // re-home everything under the drop target folder. Both steps skip
            // the individual files/folders they can't take rather than failing
            // the drop, so a partial tree still lands.
            const { files: dropped, emptyDirs } = await collectDroppedFiles(capture);
            const under = (rel: string) => (targetPath ? `${targetPath}/${rel}` : rel);
            const ingested = await ingestFilesToVirtualFiles(dropped);
            // A dropped folder named `_runs` at the root would land under
            // `_runs/` — never the user's to write.
            const additions: VirtualFile[] = ingested
                .map(vf => ({ ...vf, name: under(vf.name) }))
                .filter(vf => !isRunPath(vf.name));
            const dirs = emptyDirs.map(under).filter(d => !isRunPath(d));
            if (additions.length === 0 && dirs.length === 0) return;

            // Merged into the cell's list as it is NOW: the walk and the blob
            // uploads above can take minutes on a large drop, and whatever a
            // run or another surface wrote meanwhile stays.
            await onUpdateFiles(current => {
                const updatedFiles = mergeByName(current, additions);
                // Empty folders in the dropped tree survive as `.collkeep`
                // markers — computed against the POST-merge set so a folder
                // that did receive files (or already had some) never picks up
                // a redundant marker.
                const markers = folderMarkersFor(dirs, updatedFiles);
                // A drop that yielded nothing usable (every file unreadable, or
                // a folder-only drop whose folders all already exist) leaves
                // the cell alone rather than pushing an identical file set
                // through the draft pipeline.
                if (additions.length === 0 && markers.length === 0) return current;
                return [...updatedFiles, ...markers];
            });
        } catch (err) {
            // Nothing above is expected to throw — per-file failures are
            // already isolated — but this promise is fired and forgotten by
            // every drop site (a DOM drop handler can't await), so an escape
            // here would be an unhandled rejection: no cell change, no console
            // trail the user can act on, no feedback at all. Report it instead.
            console.error('[drop] importing the dropped files failed', err);
            void useCollimateStore.getState().openAlert({ cellId,
                title: 'Drop failed',
                body: 'The dropped files could not be imported.',
            });
        }
    }, [onUpdateFiles, readOnly]);

    return { handleCreateFileAtPath, handleCreateFolder, handleDuplicate, handleRename, handleDelete, handleDropFiles };
}
