import type { FileDescriptor } from '@/lib/fileProtocol';
import { fixtureText } from '@/test/fileFixtures';
import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, vi, beforeEach } from 'vitest';

const { readPinnedText } = vi.hoisted(() => ({ readPinnedText: vi.fn() }));
vi.mock('@/lib/fileBodies', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/fileBodies')>()),
    readPinnedText,
}));

import {
    collectTransferEntries, rebaseEntries, mergeByName,
    isInvalidSameCellTarget, parentOf, basename,
    fetchTextBodies, summarize, withFetchedBodies,
} from '@/lib/fileTransfer';
import { FOLDER_KEEP_FILE } from '@/lib/fileTree';
import type { VirtualFile } from '@/lib/types';

beforeEach(() => { readPinnedText.mockReset(); });

const file = (name: string, content = 'c'): VirtualFile => (fileFixture(name, content, {language: 'text', timestamp: ''}));

describe('fileTransfer path helpers', () => {
    it('parentOf / basename', () => {
        expect(parentOf('a/b/c.ts')).toBe('a/b');
        expect(parentOf('top.ts')).toBe('');
        expect(basename('a/b/c.ts')).toBe('c.ts');
        expect(basename('top.ts')).toBe('top.ts');
    });
});

describe('collectTransferEntries', () => {
    it('a file yields one entry rooted at its basename', () => {
        const files = [file('src/a.ts')];
        const entries = collectTransferEntries(files, 'src/a.ts', false);
        expect(entries).toHaveLength(1);
        expect(entries[0].relPath).toBe('a.ts');
    });

    it('a folder yields every descendant relative to its parent (folder name preserved)', () => {
        const files = [file('src/components/Button.tsx'), file('src/components/sub/x.ts'), file('src/other.ts')];
        const entries = collectTransferEntries(files, 'src/components', true);
        expect(entries.map(e => e.relPath).sort()).toEqual(['components/Button.tsx', 'components/sub/x.ts']);
    });

    it('an empty folder carries its marker so it survives the round-trip', () => {
        const files = [file(`src/empty/${FOLDER_KEEP_FILE}`, '')];
        const entries = collectTransferEntries(files, 'src/empty', true);
        expect(entries.map(e => e.relPath)).toEqual([`empty/${FOLDER_KEEP_FILE}`]);
    });
});

describe('rebaseEntries', () => {
    it('rebases a file into a target folder', () => {
        const entries = collectTransferEntries([file('src/a.ts', 'hi')], 'src/a.ts', false);
        const { files: out } = rebaseEntries(entries, 'lib', new Set());
        expect(out.map(f => f.name)).toEqual(['lib/a.ts']);
        expect(fixtureText(out[0])).toBe('hi');
    });

    it('rebases a folder subtree under the target', () => {
        const src = [file('src/components/Button.tsx'), file('src/components/sub/x.ts')];
        const entries = collectTransferEntries(src, 'src/components', true);
        const { files: out } = rebaseEntries(entries, 'lib', new Set());
        expect(out.map(f => f.name).sort()).toEqual(['lib/components/Button.tsx', 'lib/components/sub/x.ts']);
    });

    it('auto-renames a colliding file with (copy)', () => {
        const entries = collectTransferEntries([file('a.ts')], 'a.ts', false);
        const { files: out, renamed } = rebaseEntries(entries, '', new Set(['a.ts']));
        expect(renamed).toBe(true);
        expect(out.map(f => f.name)).toEqual(['a (copy).ts']);
    });

    it('auto-renames a colliding folder, shifting the whole subtree', () => {
        const src = [file('components/Button.tsx'), file('components/x.ts')];
        const entries = collectTransferEntries(src, 'components', true);
        const existing = new Set(['components/old.ts']);
        const { files: out } = rebaseEntries(entries, '', existing);
        expect(out.map(f => f.name).sort()).toEqual(['components (copy)/Button.tsx', 'components (copy)/x.ts']);
    });

    it('a single file colliding with an existing folder is renamed to (copy)', () => {
        const entries = collectTransferEntries([file('x')], 'x', false);
        const { files: out, renamed } = rebaseEntries(entries, '', new Set(['x/inner.md']));
        expect(renamed).toBe(true);
        expect(out.map(f => f.name)).toEqual(['x (copy)']);
    });

    it('escalates the copy suffix on repeated collisions', () => {
        const entries = collectTransferEntries([file('a.ts')], 'a.ts', false);
        const { files: out } = rebaseEntries(entries, '', new Set(['a.ts', 'a (copy).ts']));
        expect(out.map(f => f.name)).toEqual(['a (copy 2).ts']);
    });
});

describe('mergeByName', () => {
    it('mergeByName upserts by name', () => {
        const base = [file('a.ts', 'old'), file('b.ts')];
        const merged = mergeByName(base, [file('a.ts', 'new'), file('c.ts')]);
        expect(fixtureText(merged.find(f => f.name === 'a.ts')!)).toBe('new');
        expect(merged.map(f => f.name).sort()).toEqual(['a.ts', 'b.ts', 'c.ts']);
    });

    it('mergeByName replaces in place and lets a later duplicate addition win one slot', () => {
        const base = [file('a.ts', 'old'), file('b.ts')];
        const merged = mergeByName(base, [file('c.ts', '1'), file('a.ts', 'new'), file('c.ts', '2')]);
        expect(merged.map(f => f.name)).toEqual(['a.ts', 'b.ts', 'c.ts']);
        expect(merged.map(f => fixtureText(f))).toEqual(['new', 'c', '2']);
    });
});

describe('isInvalidSameCellTarget', () => {
    it('blocks a move into self, a descendant, or a no-op into the current parent', () => {
        expect(isInvalidSameCellTarget('src', 'src')).toBe(true);          // into itself
        expect(isInvalidSameCellTarget('src', 'src/components')).toBe(true); // into a descendant
        expect(isInvalidSameCellTarget('src/a.ts', 'src')).toBe(true);     // no-op: already there
        expect(isInvalidSameCellTarget('src/a.ts', 'lib')).toBe(false);    // valid move
        expect(isInvalidSameCellTarget('src', 'lib')).toBe(false);         // valid move
    });
});

it('rebases metadata without loading bytes or changing immutable identity', () => {
 const file = fileFixture('big.md', 'body', {file_revision:9});
 const moved = rebaseEntries([{relPath:'big.md',file}], 'dst', new Set()).files[0];
 expect(moved).toEqual({...file,name:'dst/big.md'});expect(moved).not.toHaveProperty('content');
});

// One hydrator reads immutable text descriptors, with bounded concurrency.
describe('fetchTextBodies (F0884, F0863)', () => {
    const metadataFile = (name: string): VirtualFile => (fileFixture(name, '', {language: 'text', timestamp: ''}));

    it('fetches metadata-only text files and skips binary, per file, with the caller\'s init', async () => {
        readPinnedText.mockImplementation(async (_ch: string, _cell: string, descriptor: FileDescriptor) => `body of ${descriptor.path}`);
        const bodies = await fetchTextBodies('ch', 'cell', [
            metadataFile('a.md'), file('b.md'),
            fileFixture('img.png', '', {language: 'binary', timestamp: '', body_hash: 'h1', encoding: 'binary'}),
        ], { cache: 'no-store' });
        expect([...bodies]).toEqual([['a.md', 'body of a.md'], ['b.md', 'body of b.md']]);
        expect(readPinnedText).toHaveBeenCalledTimes(2);
        expect(readPinnedText).toHaveBeenCalledWith('ch', 'cell', expect.objectContaining({path:'a.md'}), { cache: 'no-store' });
    });

    it('keeps at most four fetches in flight', async () => {
        let inFlight = 0;
        let peak = 0;
        readPinnedText.mockImplementation(async (_ch: string, _cell: string, descriptor: FileDescriptor) => {
            inFlight += 1;
            peak = Math.max(peak, inFlight);
            await new Promise(r => setTimeout(r, 1));
            inFlight -= 1;
            return descriptor.path;
        });
        const names = Array.from({ length: 10 }, (_, i) => `f${i}.md`);
        const bodies = await fetchTextBodies('ch', 'cell', names.map(metadataFile));
        expect(bodies.size).toBe(10);
        expect(peak).toBe(4);
    });

    it('rejects when a fetch fails and starts no further fetches', async () => {
        // One failure among slow successes: the three other workers finish
        // the fetch they are on and take no next one.
        readPinnedText.mockImplementation(async (_ch: string, _cell: string, descriptor: FileDescriptor) => {
            if (descriptor.path === 'f0.md') throw new Error('HTTP 500');
            await new Promise(r => setTimeout(r, 5));
            return descriptor.path;
        });
        await expect(fetchTextBodies('ch', 'cell', Array.from({ length: 10 }, (_, i) => metadataFile(`f${i}.md`))))
            .rejects.toThrow('HTTP 500');
        await new Promise(r => setTimeout(r, 30));
        expect(readPinnedText).toHaveBeenCalledTimes(4);
    });
});

// A write that waited on a body fetch commits against the cell as it is
// then: a fetched body applies only to its exact revision and hash.
describe('withFetchedBodies pins revision and hash', () => {
 const source=fileFixture('big.md','BODY',{file_revision:7});
 const load=withFetchedBodies([source],new Map([['big.md','BODY']]));
 it('hydrates the exact descriptor and preserves metadata',()=>{
  expect(load(source)).toEqual({...source,content:'BODY'});
 });
 it('refuses a newer revision even when bytes match',()=>{
  expect(load({...source,file_revision:8})).toBeNull();
 });
 it('refuses different bytes, unobserved files and missing bodies',()=>{
  expect(load({...source,body_hash:'different'})).toBeNull();
  expect(load(fileFixture('other.md','BODY'))).toBeNull();
  expect(withFetchedBodies([source],new Map())(source)).toBeNull();
 });
 it('returns binary metadata without reading text',()=>{
  const binary=fileFixture('image.png','',{body_hash:'binary-hash',encoding:'binary'});
  expect(load(binary)).toBe(binary);
 });
});

// Nothing lands under `_runs/` through a rebase: a user folder of that name
// moved to the cell root lands renamed, so no write is dropped at the cell
// writer after its source was removed.
describe('rebaseEntries never lands under `_runs/`', () => {
    it('a `_runs` top segment at the root is renamed; inside a folder it is an ordinary name', () => {
        const entries = collectTransferEntries([file('foo/_runs/a.txt')], 'foo/_runs', true);
        expect(rebaseEntries(entries, '', new Set()).files.map(f => f.name)).toEqual(['_runs (copy)/a.txt']);
        expect(rebaseEntries(entries, 'dst', new Set()).files.map(f => f.name)).toEqual(['dst/_runs/a.txt']);
    });
});

describe('summarize (F0861)', () => {
    it('lists at most eight names, then the count left', () => {
        expect(summarize(Array.from({ length: 10 }, (_, i) => `f${i}`))).toBe('f0, f1, f2, f3, f4, f5, f6, f7, …(+2)');
    });
});
