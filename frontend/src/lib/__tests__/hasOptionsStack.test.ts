import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect } from 'vitest';
import { hasOptionsStack } from '@/lib/fileTree';
import type { VirtualFile } from '@/lib/types';
const f = (name: string): VirtualFile => (fileFixture(name, '', {language: 'plaintext', timestamp: ''}));
describe('hasOptionsStack', () => {
    it.each(['.options_stack', 'choice.options_stack', 'folder/choice.options_stack'])('accepts declaration %s', name => {
        expect(hasOptionsStack({files: [f(name), f('README.md')]})).toBe(true);
    });
    it.each([[], ['folder/main.py'], ['folder/.collkeep'], ['README.md'], ['options_stack.txt']].map(names => ({names})))('requires a declaration rather than folder shape', ({names}) => {
        expect(hasOptionsStack({files: names.map(f)})).toBe(false);
    });
});
