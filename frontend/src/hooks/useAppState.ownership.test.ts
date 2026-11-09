import { expect, it } from 'vitest';
import { readFileSync } from 'node:fs';

it('F0957: the app-state hook leaves core type ownership in lib/types', () => {
    const source = readFileSync('src/hooks/useAppState.ts', 'utf8');
    expect(source).not.toMatch(/export\s+type\s*\{/);
});
