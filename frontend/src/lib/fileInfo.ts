// What the Files panel's Info step says about one file beyond its metadata:
// its size in words, the size itself (read without fetching a binary
// body), the journal events that name it, and when it came and last
// changed by them.

import type { JournalEvent, JournalKind } from '@/lib/api/cells';
import { descriptorOf, pinnedFileUrl, readPinnedText } from '@/lib/fileBodies';
import type { VirtualFile } from '@/lib/types';
import { runnerName } from '@/components/overview/journalText';

/** "512 B", "1.5 KB", "12 MB": one decimal under ten units. */
export function formatBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let value = bytes / 1024;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

/** The total a Content-Range names after its slash: "bytes 0-0/1234" (an
 *  empty body's 416 names its 0 the same way). */
const rangeTotal = (header: string | null): number | null => {
    const total = header?.match(/\/(\d+)\s*$/)?.[1];
    return total === undefined ? null : Number(total);
};

/** A file's size in bytes. A text body comes through the pinned-body cache
 *  (an open editor has usually read it already) and counts as UTF-8; a
 *  binary body is asked for one byte, and the cell file route's
 *  Content-Range names the whole. A 200 without one (no range support on
 *  the way) is counted as it arrives. */
export async function fileSizeOf(channel: string, cellId: string, file: VirtualFile): Promise<number> {
    if (file.encoding === 'utf-8') return new TextEncoder().encode(await readPinnedText(channel, cellId, descriptorOf(file))).byteLength;
    const response = await fetch(pinnedFileUrl(channel, cellId, file.name, file.file_revision), { headers: { Range: 'bytes=0-0' }, cache: 'no-store' });
    const total = rangeTotal(response.headers.get('Content-Range'));
    if (total !== null) return total;
    if (response.status === 200) return (await response.arrayBuffer()).byteLength;
    throw new Error(`Could not read ${file.name}: HTTP ${response.status}`);
}

/** The event names `path`: as a file it touched, or one its run wrote. */
const names = (event: JournalEvent, path: string) => event.files.includes(path) || !!event.run?.files_written.includes(path);
/** The event's file list was cut at the journal's cap: it may have touched
 *  a file it does not name. */
const cut = (event: JournalEvent) => event.files_total > event.files.length;

/** The events that name `path`, in their order (the journal's: newest
 *  first). */
export function fileEvents(events: readonly JournalEvent[], path: string): JournalEvent[] {
    return events.filter(event => names(event, path));
}

/** A time a page of the journal gives for a file: an event's, exact, or
 *  only a bound on it. */
export interface FileTime {
    t: string;
    exact: boolean;
}

/** When `path` last changed, by a page of the cell's journal (newest
 *  first): the newest event naming it, exact unless a newer event's file
 *  list was cut (that event may have changed it unnamed: then no earlier
 *  than this). Null when no event names it. */
export function lastChange(page: readonly JournalEvent[], path: string): FileTime | null {
    const at = page.findIndex(event => names(event, path));
    return at < 0 ? null : { t: page[at].t, exact: !page.slice(0, at).some(cut) };
}

/** When `path` came to be, by a page of the cell's journal (newest first):
 *  the oldest event naming it (a rename brings a file in under its new
 *  name). Exact only when the page reaches the cell's making and no older
 *  event's file list was cut (it may have brought the file in unnamed);
 *  else no later than this. A cell a move made (a duplicate, merge, split
 *  or run in a new cell) has no making and its move names no files, so its
 *  files' arrival is only ever bounded. Null when no event names it. */
export function firstChange(page: readonly JournalEvent[], path: string): FileTime | null {
    const at = page.findLastIndex(event => names(event, path));
    if (at < 0) return null;
    return { t: page[at].t, exact: page.some(event => event.kind === 'make') && !page.slice(at + 1).some(cut) };
}

const KIND_LABEL: Record<JournalKind, string> = {
    make: 'Made',
    edit: 'Edited',
    run: 'Run',
    duplicate: 'Duplicated',
    merge: 'Merged',
    split: 'Split',
    run_cell: 'Run in new cell',
    cell_renamed: 'Renamed',
};

/** Who made an event: a run's runner, else "you" or the runner that
 *  acted. */
const actorOf = ({ run, by }: JournalEvent): string =>
    run ? runnerName(run) : by.kind === 'user' ? 'you' : by.runner_name || by.runner_id || 'a runner';

/** An event in a few words: "Edited by you", "Run by Writer". */
export function eventLabel(event: JournalEvent): string {
    return `${KIND_LABEL[event.kind]} by ${actorOf(event)}`;
}
