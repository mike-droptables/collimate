import { describe, it, expect } from 'vitest';
import { languageForPath } from '@/lib/fileLanguage';

describe('languageForPath', () => {
    it('maps the basename extension, case-insensitively', () => {
        expect(languageForPath('a/b.js')).toBe('javascript');
        expect(languageForPath('x.YML')).toBe('yaml');
        expect(languageForPath('n.md')).toBe('markdown');
    });

    it('a name without an extension, or an unknown one, is plaintext', () => {
        expect(languageForPath('dir.v2/README')).toBe('plaintext');
        expect(languageForPath('Makefile')).toBe('plaintext');
        expect(languageForPath('.env')).toBe('plaintext');
        expect(languageForPath('data.unknownext')).toBe('plaintext');
    });
});
