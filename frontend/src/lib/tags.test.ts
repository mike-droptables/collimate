import { describe, expect, it } from 'vitest';
import {
    TAG_MAX_LENGTH, TAGS_PER_CELL_MAX, foldTag, hasTag, mergeVocabulary, normalizeTag, normalizeTags, tagMatches,
} from './tags';

// The client mirror of api/cell_tags.py: the same two limits (generated
// from it by frontend/scripts/sync_app_constants.py, whose --check
// api/tests/test_pytest_client_constants.py runs), the same normalizer, the
// matcher the TagMenu and the shelf search share, and the vocabulary merge.

describe('the tag rules (TG-01)', () => {
    it('a tag is at most 48 characters and a cell holds at most 32 (TG-01)', () => {
        expect([TAG_MAX_LENGTH, TAGS_PER_CELL_MAX]).toEqual([48, 32]);
    });

    it('trims, collapses inner whitespace and refuses empty, control and overlong tags (TG-01)', () => {
        expect(normalizeTag('  needs   review ')).toBe('needs review');
        expect(normalizeTag('a\tb\n c')).toBe('a b c');
        // Python's whitespace: the file separators collapse, a BOM stays.
        expect(normalizeTag('a\x1cb')).toBe('a b');
        expect(normalizeTag('﻿a')).toBe('﻿a');
        expect(normalizeTag('a　 b')).toBe('a b');
        expect(normalizeTag('')).toBeNull();
        expect(normalizeTag('   ')).toBeNull();
        expect(normalizeTag('bad\x00tag')).toBeNull();
        expect(normalizeTag('bell\x07')).toBeNull();
        expect(normalizeTag('del\x7f')).toBeNull();
        expect(normalizeTag('x'.repeat(TAG_MAX_LENGTH))).toBe('x'.repeat(TAG_MAX_LENGTH));
        expect(normalizeTag('x'.repeat(TAG_MAX_LENGTH + 1))).toBeNull();
        // Code points, as the server counts: 48 astral characters fit.
        expect(normalizeTag('\u{1F600}'.repeat(TAG_MAX_LENGTH))).not.toBeNull();
    });

    it('a list keeps its order and the first spelling of a casefold duplicate (TG-01)', () => {
        expect(normalizeTags([' Draft', 'review', 'DRAFT', 'Review ', 'straße', 'STRASSE'])).toEqual(['Draft', 'review', 'straße']);
        expect(foldTag('Straße')).toBe(foldTag('STRASSE'));
        expect(hasTag(['Draft'], 'draft')).toBe(true);
        expect(hasTag(['Draft'], 'raft')).toBe(false);
    });

    it('a list holds at most the cap and only strings (TG-01)', () => {
        const many = Array.from({ length: TAGS_PER_CELL_MAX }, (_, i) => `t${i}`);
        expect(normalizeTags(many)).toHaveLength(TAGS_PER_CELL_MAX);
        // Duplicates do not count against the cap.
        expect(normalizeTags([...many, 'T0'])).toHaveLength(TAGS_PER_CELL_MAX);
        expect(() => normalizeTags([...many, 'one more'])).toThrow(RangeError);
        expect(() => normalizeTags('a')).toThrow(TypeError);
        expect(() => normalizeTags(['a', 3])).toThrow(TypeError);
        // A refused tag refuses the list, as the server's 400 does.
        expect(() => normalizeTags(['a', '  '])).toThrow(RangeError);
        expect(() => normalizeTags(['x'.repeat(TAG_MAX_LENGTH + 1)])).toThrow(RangeError);
    });
});

describe('matching and the vocabulary (TG-05)', () => {
    it('a query matches a casefold substring; an empty one matches every tag (TG-05)', () => {
        expect(tagMatches('VIEW', 'needs review')).toBe(true);
        expect(tagMatches('  needs   rev ', 'Needs Review')).toBe(true);
        expect(tagMatches('draft', 'needs review')).toBe(false);
        expect(tagMatches('', 'anything')).toBe(true);
    });

    it('merges the board, the shelf and the route, unique under casefold, sorted casefold (TG-05)', () => {
        const vocabulary = mergeVocabulary(
            [{ tags: ['beta', 'Alpha'] }, {}],
            [{ tags: ['ALPHA', 'gamma'] }],
            [{ tag: 'Delta' }, { tag: 'beta' }],
        );
        expect(vocabulary).toEqual(['Alpha', 'beta', 'Delta', 'gamma']);
        expect(mergeVocabulary([], null, null)).toEqual([]);
    });
});
