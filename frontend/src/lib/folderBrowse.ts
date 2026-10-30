// One folder of a cell's files as the Files panel's Browse/Create mode lists
// it: the folder's direct children, folders first, each by name (the
// tree's order, lib/fileSearch treeNodeOrder), filtered by the field's
// query on their names. Pure.

import { compileQuery } from '@/lib/fileSearch';
import { isFolderKeep } from '@/lib/fileTree';
import type { VirtualFile } from '@/lib/types';

/** One child of the browsed folder. `path` is cell-relative; a file entry
 *  carries its file. */
export interface BrowseEntry {
    path: string;
    name: string;
    kind: 'folder' | 'file';
    file?: VirtualFile;
}

/** How a location line names the cell's top folder (''). */
export const ROOT_LABEL = 'root';

/** The folder above `folder` ('' at and under the root). */
export function parentFolder(folder: string): string {
    const slash = folder.lastIndexOf('/');
    return slash < 0 ? '' : folder.slice(0, slash);
}

/** The direct children of `folder` ('' = the root): its subfolders, then
 *  its files, each by name. A `.collkeep` marker makes its (empty) folder
 *  show but is never an entry itself. `query` keeps the entries whose NAME
 *  it matches (compileQuery's rules on the basename: a substring or a glob). */
export function folderEntries(files: readonly VirtualFile[], folder: string, query: string): BrowseEntry[] {
    const prefix = folder ? `${folder}/` : '';
    const folders = new Map<string, BrowseEntry>();
    const children: BrowseEntry[] = [];
    for (const file of files) {
        if (!file.name.startsWith(prefix)) continue;
        const rest = file.name.slice(prefix.length);
        const slash = rest.indexOf('/');
        if (slash >= 0) {
            const name = rest.slice(0, slash);
            if (!folders.has(name)) folders.set(name, { path: prefix + name, name, kind: 'folder' });
        } else if (!isFolderKeep(rest)) {
            children.push({ path: file.name, name: rest, kind: 'file', file });
        }
    }
    const matches = compileQuery(query);
    const byName = (a: BrowseEntry, b: BrowseEntry) => a.name.localeCompare(b.name);
    return [...[...folders.values()].sort(byName), ...children.sort(byName)].filter(entry => matches(entry.name));
}
