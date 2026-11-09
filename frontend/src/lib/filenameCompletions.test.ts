// Inline extension completion (lib/filenameCompletions): the suffixes a
// typed name can grow by, at every dot, never changing what was typed.

import { describe, expect, it } from 'vitest';
import { filenameCompletions } from '@/lib/filenameCompletions';

const EXTS = [{ ext: '.md' }, { ext: '.mdx' }, { ext: '.tar.gz' }, { ext: '.py' }, { ext: 'noDot' }];

describe('filenameCompletions', () => {
    it('completes a partly typed extension after the last dot', () => {
        expect(filenameCompletions('notes.m', EXTS)).toEqual(['notes.md', 'notes.mdx']);
        expect(filenameCompletions('run.p', EXTS)).toEqual(['run.py']);
    });

    it('offers nothing before a dot or for a whole extension, and only longer ones after a known one', () => {
        expect(filenameCompletions('notes', EXTS)).toEqual([]);
        expect(filenameCompletions('run.py', EXTS)).toEqual([]);
        expect(filenameCompletions('notes.md', EXTS)).toEqual(['notes.mdx']);
    });

    it('completes a compound extension from any dot in it and keeps the typed case', () => {
        expect(filenameCompletions('Data.tar.g', EXTS)).toEqual(['Data.tar.gz']);
        expect(filenameCompletions('Data.T', EXTS)).toEqual(['Data.Tar.gz']);
    });

    it('ignores extensions that do not start with a dot', () => {
        expect(filenameCompletions('x.n', EXTS)).toEqual([]);
    });
});
