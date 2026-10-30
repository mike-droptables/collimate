import { describe, it, expect } from 'vitest';
import { currentFirst, diffLines, diffStats } from '../textDiff';

const render = (rows: ReturnType<typeof diffLines>) =>
    rows.map(r => `${r.kind[0]}:${r.left ?? ''}|${r.right ?? ''}`);

describe('diffLines', () => {
    it('marks identical texts as all-same with paired line numbers', () => {
        const rows = diffLines('a\nb\nc', 'a\nb\nc');
        expect(rows).toHaveLength(3);
        expect(rows.every(r => r.kind === 'same')).toBe(true);
        expect(rows[2]).toMatchObject({ leftNo: 3, rightNo: 3 });
    });

    it('detects a single changed line as del+add', () => {
        const rows = diffLines('a\nb\nc', 'a\nX\nc');
        expect(render(rows)).toEqual(['s:a|a', 'd:b|', 'a:|X', 's:c|c']);
    });

    it('detects insertions and deletions with correct line numbers', () => {
        const rows = diffLines('a\nc', 'a\nb\nc');
        expect(render(rows)).toEqual(['s:a|a', 'a:|b', 's:c|c']);
        const add = rows.find(r => r.kind === 'add')!;
        expect(add).toMatchObject({ leftNo: null, rightNo: 2 });

        const rows2 = diffLines('a\nb\nc', 'a\nc');
        expect(render(rows2)).toEqual(['s:a|a', 'd:b|', 's:c|c']);
        const del = rows2.find(r => r.kind === 'del')!;
        expect(del).toMatchObject({ leftNo: 2, rightNo: null });
    });

    it('recovers common lines in the middle of changes (real LCS)', () => {
        const rows = diffLines('x\nkeep\ny', 'p\nkeep\nq');
        expect(render(rows)).toEqual(['d:x|', 'a:|p', 's:keep|keep', 'd:y|', 'a:|q']);
    });

    it('diffs against the empty string', () => {
        expect(render(diffLines('', 'a\nb'))).toEqual(['d:|', 'a:|a', 'a:|b']);
        expect(render(diffLines('a', ''))).toEqual(['d:a|', 'a:|']);
        expect(render(diffLines('', ''))).toEqual(['s:|']);
    });

    it('handles duplicate lines without mispairing', () => {
        const rows = diffLines('a\na\nb', 'a\nb');
        const same = rows.filter(r => r.kind === 'same');
        expect(same).toHaveLength(2);
        expect(rows.filter(r => r.kind === 'del')).toHaveLength(1);
    });

    it('falls back to block replacement above the size cap without hanging', () => {
        const a = Array.from({ length: 3000 }, (_, i) => `left ${i}`).join('\n');
        const b = Array.from({ length: 3000 }, (_, i) => `right ${i}`).join('\n');
        const rows = diffLines(a, b);
        expect(rows.filter(r => r.kind === 'del')).toHaveLength(3000);
        expect(rows.filter(r => r.kind === 'add')).toHaveLength(3000);
    });

    it('currentFirst puts each changed run\'s current lines ahead of its base lines', () => {
        const rows = diffLines('x\nkeep\ny\nz', 'p\nkeep\nq');
        expect(render(currentFirst(rows))).toEqual(['a:|p', 'd:x|', 's:keep|keep', 'a:|q', 'd:y|', 'd:z|']);
        expect(render(currentFirst(diffLines('a\nb', 'a\nb')))).toEqual(['s:a|a', 's:b|b']);
        expect(currentFirst([])).toEqual([]);
    });

    it('diffStats counts adds and removals', () => {
        const rows = diffLines('a\nb\nc', 'a\nX\nY\nc');
        expect(diffStats(rows)).toEqual({ added: 2, removed: 1 });
    });
});
