import { describe, expect, it } from 'vitest';
import { normalizeReplacement, replacementNote, replacementText, versionReplacements } from './replacements';

describe('replacement marks', () => {
    it('say whose revision a run replaced on purpose', () => {
        const user = normalizeReplacement({ path: 'notes.md', revision: 3, writer_id: 'w', writer: { kind: 'user' } })!;
        expect(replacementText(user)).toBe('Replaced your revision 3 of notes.md deliberately');
        expect(replacementNote(user)).toBe('replaced your revision 3 deliberately');
        const run = normalizeReplacement({ revision: 4, writer_id: 'run:r:w',
            writer: { kind: 'run', run_id: 'r', label: 'agent-x-12345678', generator: 'agent' } }, 'a.md')!;
        expect(replacementText(run)).toBe('Replaced revision 4 of a.md by run agent-x-12345678 deliberately');
        const host = normalizeReplacement({ path: 'a.md', revision: 2, writer: { kind: 'host' } })!;
        expect(replacementText(host)).toBe("Replaced the host's revision 2 of a.md deliberately");
    });

    it('drop marks that name no revision', () => {
        expect(normalizeReplacement({ path: 'a.md', writer: { kind: 'user' } })).toBeNull();
        expect(normalizeReplacement({ path: 'a.md', revision: 0 })).toBeNull();
        expect(versionReplacements([{ path: 'a.md', revision: 2, writer: null }, 'junk'])).toHaveLength(1);
        expect(versionReplacements(undefined)).toEqual([]);
    });
});
