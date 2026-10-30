// A file's facts for the Files panel's Info step (lib/fileInfo): sizes in
// words, the size of a text body (UTF-8 bytes) or a binary one (from the
// route's Content-Range), the journal events naming a file, their labels,
// and when a file came and last changed by them.

import { afterEach, describe, expect, it, vi } from 'vitest';
import { binaryFile } from '@/lib/fileCommands';
import { eventLabel, fileEvents, fileSizeOf, firstChange, formatBytes, lastChange } from '@/lib/fileInfo';
import { pinnedFileUrl } from '@/lib/fileBodies';
import type { JournalEvent, JournalRun } from '@/lib/api/cells';
import { fileFixture } from '@/test/fileFixtures';

afterEach(() => { vi.unstubAllGlobals(); });

const run = (over: Partial<JournalRun> = {}): JournalRun => ({
    run_id: 'r1', runner_id: 'runner-1', runner_name: 'Writer', status: 'completed', outcome: null, note: null,
    started_at: null, ended_at: null, files_written: [], ...over,
});
const event = (over: Partial<JournalEvent> = {}): JournalEvent => ({
    t: '2026-10-01T10:00:00Z', kind: 'edit', by: { kind: 'user', runner_id: null, runner_name: null },
    cells: [], run: null, files: [], files_total: 0, version_id: null, ...over,
});

describe('formatBytes', () => {
    it('bytes under 1 KB, one decimal under ten units, whole numbers above', () => {
        expect(formatBytes(0)).toBe('0 B');
        expect(formatBytes(1023)).toBe('1023 B');
        expect(formatBytes(1536)).toBe('1.5 KB');
        expect(formatBytes(20 * 1024)).toBe('20 KB');
        expect(formatBytes(3 * 1024 * 1024)).toBe('3.0 MB');
        expect(formatBytes(2048 * 1024 ** 4)).toBe('2048 TB');
    });
});

describe('fileSizeOf', () => {
    it('counts a text body\'s UTF-8 bytes from the pinned-body cache, without a request', async () => {
        const fetcher = vi.fn();
        vi.stubGlobal('fetch', fetcher);
        expect(await fileSizeOf('ch', 'c1', fileFixture('notes.md', 'héllo ✓'))).toBe(10);
        expect(fetcher).not.toHaveBeenCalled();
    });

    it('asks a binary body for one byte and reads the total from Content-Range', async () => {
        const fetcher = vi.fn(async () => new Response(new Uint8Array([7]), { status: 206, headers: { 'Content-Range': 'bytes 0-0/48213' } }));
        vi.stubGlobal('fetch', fetcher);
        const file = { ...binaryFile('img/logo.png', 'abc'), file_revision: 3 };
        expect(await fileSizeOf('ch', 'c1', file)).toBe(48213);
        expect(fetcher).toHaveBeenCalledExactlyOnceWith(pinnedFileUrl('ch', 'c1', 'img/logo.png', 3), { headers: { Range: 'bytes=0-0' }, cache: 'no-store' });
    });

    it('an empty binary body\'s 416 names its size too', async () => {
        vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 416, headers: { 'Content-Range': 'bytes */0' } })));
        expect(await fileSizeOf('ch', 'c1', binaryFile('empty.bin', 'e'))).toBe(0);
    });

    it('a 200 without Content-Range is counted whole', async () => {
        vi.stubGlobal('fetch', vi.fn(async () => new Response(new Uint8Array(1500), { status: 200 })));
        expect(await fileSizeOf('ch', 'c1', binaryFile('blob.bin', 'b'))).toBe(1500);
    });

    it('a failed request is an error, never a size', async () => {
        vi.stubGlobal('fetch', vi.fn(async () => new Response('gone', { status: 404 })));
        await expect(fileSizeOf('ch', 'c1', binaryFile('gone.bin', 'g'))).rejects.toThrow('HTTP 404');
    });
});

describe('fileEvents', () => {
    it('keeps the events naming the path as a file or a run\'s written file, in their order', () => {
        const edit = event({ files: ['a.md', 'b.md'] });
        const other = event({ files: ['c.md'] });
        const wrote = event({ kind: 'run', run: run({ files_written: ['a.md'] }) });
        const ranOther = event({ kind: 'run', run: run({ files_written: ['c.md'] }) });
        const made = event({ kind: 'make', files: ['a.md'] });
        expect(fileEvents([edit, other, wrote, ranOther, made], 'a.md')).toEqual([edit, wrote, made]);
        expect(fileEvents([edit], 'a')).toEqual([]);
    });
});

describe('lastChange and firstChange', () => {
    const at = (hour: number, over: Partial<JournalEvent> = {}) => event({ t: `2026-10-01T${String(hour).padStart(2, '0')}:00:00Z`, ...over });
    /** A file list the journal cut at its cap: `named` of 150 paths. */
    const capped = (hour: number, named: string[] = ['other.md']) => at(hour, { files: named, files_total: 150 });

    it('the newest and the oldest event naming the file, exact on a whole history', () => {
        const page = [at(9, { files: ['b.md'] }), at(8, { kind: 'run', run: run({ files_written: ['a.md'] }) }), at(7, { files: ['a.md'] }), at(6, { kind: 'make', files: ['b.md'] })];
        expect(lastChange(page, 'a.md')).toEqual({ t: page[1].t, exact: true });
        expect(firstChange(page, 'a.md')).toEqual({ t: page[2].t, exact: true });
        expect(firstChange(page, 'b.md')).toEqual({ t: page[3].t, exact: true });
    });

    it('a newer cut file list makes the last change a lower bound', () => {
        expect(lastChange([capped(9), at(8, { files: ['a.md'] })], 'a.md')).toEqual({ t: at(8).t, exact: false });
        // An older cut list says nothing about the last change.
        expect(lastChange([at(9, { files: ['a.md'] }), capped(8)], 'a.md')).toEqual({ t: at(9).t, exact: true });
    });

    it('the arrival is only an upper bound without the cell\'s making, or with an older cut file list', () => {
        // A cell a duplicate made: no make, and the move names no files.
        expect(firstChange([at(9, { files: ['a.md'] }), at(8, { kind: 'duplicate' })], 'a.md')).toEqual({ t: at(9).t, exact: false });
        // A making whose own list was cut may have brought the file unnamed.
        expect(firstChange([at(9, { files: ['a.md'] }), { ...capped(8), kind: 'make' }], 'a.md')).toEqual({ t: at(9).t, exact: false });
        // A making whose cut list names the file is its arrival.
        expect(firstChange([at(9, { files: ['a.md'] }), { ...capped(8, ['a.md']), kind: 'make' }], 'a.md')).toEqual({ t: at(8).t, exact: true });
    });

    it('nothing when no event names the file', () => {
        const page = [at(9, { files: ['b.md'] }), at(8, { kind: 'make' })];
        expect(lastChange(page, 'a.md')).toBeNull();
        expect(firstChange(page, 'a.md')).toBeNull();
    });
});

describe('eventLabel', () => {
    it('names each kind with its actor: you, the acting runner, or a run\'s runner', () => {
        const runner = { kind: 'runner', runner_id: 'runner-9', runner_name: 'Tidier' } as const;
        expect(eventLabel(event())).toBe('Edited by you');
        expect(eventLabel(event({ by: runner }))).toBe('Edited by Tidier');
        expect(eventLabel(event({ by: { ...runner, runner_name: null } }))).toBe('Edited by runner-9');
        expect(eventLabel(event({ kind: 'make' }))).toBe('Made by you');
        expect(eventLabel(event({ kind: 'run', by: runner, run: run() }))).toBe('Run by Writer');
        expect(eventLabel(event({ kind: 'run_cell', by: runner, run: run({ runner_name: null }) }))).toBe('Run in new cell by runner-1');
        expect(eventLabel(event({ kind: 'duplicate' }))).toBe('Duplicated by you');
        expect(eventLabel(event({ kind: 'merge' }))).toBe('Merged by you');
        expect(eventLabel(event({ kind: 'split' }))).toBe('Split by you');
        expect(eventLabel(event({ kind: 'cell_renamed' }))).toBe('Renamed by you');
    });
});
