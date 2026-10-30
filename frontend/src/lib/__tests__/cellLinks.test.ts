import { describe, expect, it } from 'vitest';
import { cellPathOfHref } from '../cellLinks';

describe('cellPathOfHref', () => {
    it('names a cell file for a relative path, decoded and without its query or fragment', () => {
        expect(cellPathOfHref('./a%20b.md?x#y')).toBe('a b.md');
        expect(cellPathOfHref('/notes/a.md')).toBe('notes/a.md');
        expect(cellPathOfHref('notes/../b.md')).toBe('b.md');
        expect(cellPathOfHref('bad%zz.md')).toBe('bad%zz.md');
    });

    it('is null for a scheme, a protocol-relative or fragment-only href, nothing, or a climb out of the cell', () => {
        for (const href of ['https://example.test/a', 'mailto:x@y', 'javascript:void(0)', '//cdn.test/a.js', '#top', '', '   ', null, undefined, '../up.md', '..']) {
            expect(cellPathOfHref(href)).toBeNull();
        }
    });
});
