import { describe, expect, it } from 'vitest';
import { previewPolicy } from '../../../../builtins/definitions/renderer-example/policy.js';

// The opaque frames that load the bundled faces from the app origin's
// /fonts/ must list that origin in font-src.

function directive(policy: string, name: string): string[] {
    const found = policy.split(';').map((part) => part.trim().split(/\s+/)).find(([head]) => head === name);
    return found ? found.slice(1) : [];
}

describe('font-src in the opaque frames\' CSP', () => {
    it('the sealed renderer preview lists the API origin in font-src (S03)', () => {
        const policy = previewPolicy('http://127.0.0.1:8000/api/x', 'http://127.0.0.1:8000/fonts/fonts.css');
        expect(directive(policy, 'font-src')).toEqual(['http://127.0.0.1:8000', 'data:']);
        expect(directive(policy, 'style-src')).toEqual(["'unsafe-inline'", 'http://127.0.0.1:8000']);
        expect(directive(policy, 'default-src')).toEqual(["'none'"]);
    });
});
