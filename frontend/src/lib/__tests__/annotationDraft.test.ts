import { describe, expect, it } from 'vitest';
import { COMPOSER_FILENAME_BASE, composerFilename, draftKey, isCellDraft } from '../annotationDraft';

describe('the composer draft', () => {
    it('takes the first message file name the cell does not hold', () => {
        expect(COMPOSER_FILENAME_BASE).toBe('.collimate-message');
        expect(composerFilename([])).toBe('.collimate-message.md');
        expect(composerFilename(['notes.md', '.collimate-message.md'])).toBe('.collimate-message-2.md');
        expect(composerFilename(['.collimate-message.md', '.collimate-message-2.md'])).toBe('.collimate-message-3.md');
        expect(composerFilename(['dir/.collimate-message.md'])).toBe('.collimate-message.md');
    });

    it('keys a drilldown run\'s draft apart from the cell\'s All draft', () => {
        expect(draftKey('c1', { kind: 'run', runId: 'r1' })).toBe('c1:run:r1');
        expect(draftKey('c1', { kind: 'all' })).toBe('c1:all');
        expect(isCellDraft(draftKey('c1', { kind: 'run', runId: 'r1' }), 'c1')).toBe(true);
        expect(isCellDraft(draftKey('c1', { kind: 'all' }), 'c1')).toBe(true);
        expect(isCellDraft(draftKey('c10', { kind: 'all' }), 'c1')).toBe(false);
    });
});
