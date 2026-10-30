// What a paste into the Files pane carries, and what the pasted file is
// called. One classification serves the Ctrl+V `paste` event (a
// DataTransfer) and the context menu's "Paste here" (the async Clipboard
// API's ClipboardItems); hooks/useFilesPanePaste acts on the result.

import type { VirtualFile } from '@/lib/types';
import type { FileClipboard } from '@/stores/collimate/types';
import { isFolderKeep } from '@/lib/fileTree';
import { mergeByName, summarize } from '@/lib/fileTransfer';

/** The image types a paste lands as a file, with the extension each gets. */
export const IMAGE_EXT: Readonly<Record<string, string>> = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/svg+xml': '.svg',
};

export type TextExt = '.md' | '.txt';

export type PasteContent =
    /** The app's own copied or cut files (useFileTransfer's clipboard). */
    | { kind: 'internal' }
    /** Files copied in the OS file manager, each under its own name. */
    | { kind: 'files'; files: File[] }
    | { kind: 'image'; blob: Blob; ext: string }
    | { kind: 'text'; text: string; ext: TextExt };

/** The parts of a DataTransfer the classification reads. */
export interface PasteData {
    readonly files?: ArrayLike<File> | null;
    readonly items?: ArrayLike<{ readonly kind: string; readonly type: string; getAsFile(): File | null }> | null;
    getData(format: string): string;
}

/** True for text that reads as markdown: a heading, a list item, a quote, a
 *  code fence or a link. */
export function looksMarkdown(text: string): boolean {
    return /^#{1,6}\s+\S/m.test(text)
        || /^\s*(?:[-*+]|\d+[.)])\s+\S/m.test(text)
        || /^>\s?\S/m.test(text)
        || /^\s*(?:```|~~~)/m.test(text)
        || /\[[^\]\n]+\]\([^)\s]+\)/.test(text);
}

const pad = (n: number) => String(n).padStart(2, '0');

/** `pasted-image-YYYYMMDD-HHmmss` / `pasted-text-…`, in local time. */
export function defaultPasteStem(kind: 'image' | 'text', now: Date): string {
    const date = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;
    const time = `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    return `pasted-${kind}-${date}-${time}`;
}

/** A name split at its last dot: `report.pdf` → [`report`, `.pdf`]; a name
 *  with no extension (or a dotfile) keeps it all in the stem. */
export function splitFileName(name: string): { stem: string; ext: string } {
    const dot = name.lastIndexOf('.');
    return dot > 0 ? { stem: name.slice(0, dot), ext: name.slice(dot) } : { stem: name, ext: '' };
}

/** A bitmap on the clipboard (a screenshot, an image copied in a browser)
 *  reaches the paste event as one File the browser names `image.<ext>`
 *  after its type: it has no name of its own, so it is an image paste. */
function isClipboardBitmap(file: File): boolean {
    const ext = IMAGE_EXT[file.type];
    return !!ext && file.name.toLowerCase() === `image${ext}`;
}

/** `copiedOsText` is what the latest copy or cut wrote to the OS clipboard:
 *  once a cut paste has cleared the internal clipboard, that text is spent. */
function textContent(text: string, internal: FileClipboard | null, copiedOsText: string | null): PasteContent | null {
    if (internal && text === internal.osText) return { kind: 'internal' };
    if (text === copiedOsText) return null;
    if (text.length > 0) return { kind: 'text', text, ext: looksMarkdown(text) ? '.md' : '.txt' };
    return internal ? { kind: 'internal' } : null;
}

/** What a `paste` event carries, in precedence order: OS files, an image,
 *  text equal to the internal clipboard's marker (the internal clipboard),
 *  other text, then the internal clipboard when the OS clipboard is empty.
 *  Null when there is nothing to paste, or when the text is the spent
 *  marker of a clipboard a cut paste cleared. text/html is never read. */
export function classifyPaste(
    data: PasteData, internal: FileClipboard | null, copiedOsText: string | null,
): PasteContent | null {
    const files = Array.from(data.files ?? []);
    if (files.length === 1 && isClipboardBitmap(files[0])) {
        return { kind: 'image', blob: files[0], ext: IMAGE_EXT[files[0].type] };
    }
    if (files.length > 0) return { kind: 'files', files };
    for (const item of Array.from(data.items ?? [])) {
        if (item.kind !== 'file' || !IMAGE_EXT[item.type]) continue;
        const blob = item.getAsFile();
        if (blob) return { kind: 'image', blob, ext: IMAGE_EXT[item.type] };
    }
    return textContent(data.getData('text/plain'), internal, copiedOsText);
}

/** The same classification for the async Clipboard API ("Paste here"). It
 *  exposes only image/png, text/plain and text/html, never OS files. */
export async function classifyClipboardItems(
    items: readonly ClipboardItem[], internal: FileClipboard | null, copiedOsText: string | null,
): Promise<PasteContent | null> {
    for (const item of items) {
        const type = item.types.find(t => IMAGE_EXT[t]);
        if (type) return { kind: 'image', blob: await item.getType(type), ext: IMAGE_EXT[type] };
    }
    let text = '';
    for (const item of items) {
        if (!item.types.includes('text/plain')) continue;
        text = await (await item.getType('text/plain')).text();
        break;
    }
    return textContent(text, internal, copiedOsText);
}

/** The names among `additions` that would replace a file already in
 *  `files` (a folder's `.collkeep` marker is not a file the user sees). */
export function overwrittenNames(additions: readonly { name: string }[], files: readonly VirtualFile[]): string[] {
    const existing = new Set(files.map(f => f.name));
    return additions.filter(a => existing.has(a.name) && !isFolderKeep(a.name)).map(a => a.name);
}

/** The one question before a paste of several files replaces `names`. */
export function overwriteConfirm(names: string[], where: string) {
    return {
        title: names.length === 1 ? 'Replace a file?' : `Replace ${names.length} files?`,
        body: `${names.length === 1 ? 'A file of the same name already exists' : `${names.length} files of the same names already exist`} in ${where} and will be replaced: ${summarize(names)}`,
        confirmLabel: 'Replace',
        destructive: true,
    };
}

/** `additions` upserted into `current`, the cell as it is when the write
 *  commits, after an overwrite question that named `agreed` (asked against
 *  the list the paste started from). A same-named file the question did not
 *  name was written to the cell after it was asked: it is never replaced,
 *  and its addition is left out and listed in `kept`. */
export function mergeAgreed(
    current: VirtualFile[], additions: VirtualFile[], agreed: ReadonlySet<string>,
): { files: VirtualFile[]; kept: string[] } {
    const kept = overwrittenNames(additions, current).filter(n => !agreed.has(n));
    const skip = new Set(kept);
    const landing = kept.length ? additions.filter(a => !skip.has(a.name)) : additions;
    return { files: landing.length ? mergeByName(current, landing) : current, kept };
}
