// What a paste into the Files pane carries and what it is called (FP-05).
import { describe, expect, it } from 'vitest';
import { fileFixture } from '@/test/fileFixtures';
import type { FileClipboard } from '@/stores/collimate/types';
import {
    IMAGE_EXT, classifyClipboardItems, classifyPaste, defaultPasteStem, looksMarkdown, mergeAgreed,
    overwriteConfirm, overwrittenNames, splitFileName, type PasteData,
} from './clipboardPaste';

const internal = (osText = 'a.md'): FileClipboard => ({
    op: 'copy', label: "'a.md'", entries: [], source: { channel: 'c', cellId: 'x', nodePath: 'a.md' }, osText,
});

/** A synthetic paste event's DataTransfer. */
function data({ files = [], images = [], text = '' }: { files?: File[]; images?: File[]; text?: string } = {}): PasteData {
    return {
        files,
        items: [...files, ...images].map(file => ({ kind: 'file', type: file.type, getAsFile: () => file })),
        getData: (format: string) => (format === 'text/plain' ? text : ''),
    };
}

const png = (name = 'image.png') => new File(['PNG'], name, { type: 'image/png' });

/** A synthetic async-Clipboard item. */
const clipItem = (parts: Record<string, Blob>) => ({
    types: Object.keys(parts),
    getType: async (type: string) => parts[type],
}) as unknown as ClipboardItem;

describe('classifyPaste (FP-05)', () => {
    it('OS files come first, each under its own name (FP-05)', () => {
        const a = new File(['a'], 'a.pdf', { type: 'application/pdf' });
        const b = new File(['b'], 'b.txt', { type: 'text/plain' });
        expect(classifyPaste(data({ files: [a, b], text: 'a.pdf' }), internal('a.pdf'), null)).toEqual({ kind: 'files', files: [a, b] });
        expect(classifyPaste(data({ files: [png('photo.png')] }), null, null)).toEqual({ kind: 'files', files: [expect.any(File)] });
    });

    it('a bitmap the browser names image.<ext> is an image, not a file (FP-05)', () => {
        const shot = png();
        expect(classifyPaste(data({ files: [shot] }), null, null)).toEqual({ kind: 'image', blob: shot, ext: '.png' });
    });

    it('an image item beats text; each image type gets its extension (FP-05)', () => {
        for (const [type, ext] of Object.entries(IMAGE_EXT)) {
            const image = new File(['x'], '', { type });
            expect(classifyPaste(data({ images: [image], text: '# heading' }), null, null)).toEqual({ kind: 'image', blob: image, ext });
        }
        expect(IMAGE_EXT).toEqual({ 'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp', 'image/svg+xml': '.svg' });
        const bmp = new File(['x'], '', { type: 'image/bmp' });
        expect(classifyPaste(data({ images: [bmp], text: 'plain' }), null, null)).toEqual({ kind: 'text', text: 'plain', ext: '.txt' });
    });

    it('text equal to the internal marker is the internal clipboard; other text is text (FP-05)', () => {
        expect(classifyPaste(data({ text: 'src/a.md' }), internal('src/a.md'), null)).toEqual({ kind: 'internal' });
        expect(classifyPaste(data({ text: 'something else' }), internal('src/a.md'), null))
            .toEqual({ kind: 'text', text: 'something else', ext: '.txt' });
        expect(classifyPaste(data({ text: '- one\n- two' }), null, null)).toEqual({ kind: 'text', text: '- one\n- two', ext: '.md' });
    });

    it('a cut paste spends its marker: the same text afterwards pastes nothing (FP-05)', () => {
        expect(classifyPaste(data({ text: 'src/a.md' }), null, 'src/a.md')).toBeNull();
        expect(classifyPaste(data({ text: 'src/b.md' }), null, 'src/a.md')).toEqual({ kind: 'text', text: 'src/b.md', ext: '.txt' });
        expect(classifyPaste(data({ text: 'src/a.md' }), internal('src/a.md'), 'src/a.md')).toEqual({ kind: 'internal' });
    });

    it('an empty OS clipboard pastes the internal one, else nothing (FP-05)', () => {
        expect(classifyPaste(data(), internal(), null)).toEqual({ kind: 'internal' });
        expect(classifyPaste(data(), internal('a.md'), null)).toEqual({ kind: 'internal' });
        expect(classifyPaste(data(), null, null)).toBeNull();
    });
});

describe('classifyClipboardItems (FP-05)', () => {
    it('reads an image first, then text, against the internal marker (FP-05)', async () => {
        const image = new Blob(['PNG'], { type: 'image/png' });
        const text = new Blob(['notes'], { type: 'text/plain' });
        expect(await classifyClipboardItems([clipItem({ 'text/plain': text, 'image/png': image })], null, null))
            .toEqual({ kind: 'image', blob: image, ext: '.png' });
        expect(await classifyClipboardItems([clipItem({ 'text/html': new Blob(['<b>x</b>']), 'text/plain': text })], null, null))
            .toEqual({ kind: 'text', text: 'notes', ext: '.txt' });
        expect(await classifyClipboardItems([clipItem({ 'text/plain': text })], internal('notes'), null)).toEqual({ kind: 'internal' });
        expect(await classifyClipboardItems([], internal(), null)).toEqual({ kind: 'internal' });
        expect(await classifyClipboardItems([clipItem({ 'text/html': new Blob(['<b>x</b>']) })], null, null)).toBeNull();
        expect(await classifyClipboardItems([clipItem({ 'text/plain': text })], null, 'notes')).toBeNull();
    });
});

describe('naming (FP-05)', () => {
    it('looksMarkdown sees headings, lists, quotes, fences and links (FP-05)', () => {
        for (const text of ['# Title', 'intro\n## Part', '* item', '1. first', '> quoted', '```ts\nx\n```', 'see [docs](https://x.dev)']) {
            expect(looksMarkdown(text)).toBe(true);
        }
        for (const text of ['just a sentence.', '#hashtag', 'a - b', 'x > y', 'f(x) [1]']) {
            expect(looksMarkdown(text)).toBe(false);
        }
    });

    it('defaultPasteStem stamps the local date and time (FP-05)', () => {
        const now = new Date(2026, 9, 2, 7, 5, 9);
        expect(defaultPasteStem('image', now)).toBe('pasted-image-20261002-070509');
        expect(defaultPasteStem('text', now)).toBe('pasted-text-20261002-070509');
    });

    it('splitFileName splits at the last dot; a dotfile keeps its name (FP-05)', () => {
        expect(splitFileName('report.final.pdf')).toEqual({ stem: 'report.final', ext: '.pdf' });
        expect(splitFileName('Makefile')).toEqual({ stem: 'Makefile', ext: '' });
        expect(splitFileName('.env')).toEqual({ stem: '.env', ext: '' });
    });
});

describe('several files: one overwrite question (FP-05)', () => {
    const vf = (name: string, body = name) => fileFixture(name, body, { language: 'text', timestamp: 't' });

    it('overwrittenNames lists the names a paste replaces, never a folder marker (FP-05)', () => {
        const current = [vf('a.txt'), vf('d/.collkeep')];
        expect(overwrittenNames([{ name: 'a.txt' }, { name: 'b.txt' }, { name: 'd/.collkeep' }], current)).toEqual(['a.txt']);
        expect(overwriteConfirm(['a.txt'], 'the cell')).toMatchObject({ confirmLabel: 'Replace', destructive: true });
    });

    it('mergeAgreed replaces what was agreed and keeps a file written after the question (FP-05)', () => {
        const current = [vf('a.txt', 'old a'), vf('late.txt', 'written meanwhile')];
        const additions = [vf('a.txt', 'new a'), vf('late.txt', 'pasted'), vf('b.txt', 'new b')];
        const { files, kept } = mergeAgreed(current, additions, new Set(['a.txt']));
        expect(kept).toEqual(['late.txt']);
        expect(files.map(f => f.name)).toEqual(['a.txt', 'late.txt', 'b.txt']);
        expect(files[0]).toBe(additions[0]);
        expect(files[1]).toBe(current[1]);
    });
});
