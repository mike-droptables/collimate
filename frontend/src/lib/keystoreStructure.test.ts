import { describe, expect, it } from 'vitest';
import { sameStructure, structureFromLeaves, structureLeaves, structureProblem, valuesFromPaths, valueAt } from './keystoreStructure';

describe('keystore structures (collimate.keystore in the browser)', () => {
    it('builds a nested structure from dotted keys and reads it back in canonical order', () => {
        const structure = structureFromLeaves([{ path: 'url', secret: false }, { path: 'auth.token', secret: true }, { path: 'auth.user', secret: false }]);
        expect(structure).toEqual({ url: 'public', auth: { token: 'secret', user: 'public' } });
        expect(structureLeaves(structure)).toEqual([
            { path: 'auth.token', secret: true }, { path: 'auth.user', secret: false }, { path: 'url', secret: false }]);
    });
    it.each([
        [[{ path: 'url', secret: false }], 'at least one key secret'],
        [[{ path: 'a', secret: true }, { path: 'a.b', secret: false }], 'a is a key and cannot hold others'],
        [[{ path: 'a.b', secret: true }, { path: 'a', secret: false }], 'both a key and a group'],
        [[{ path: 'a', secret: true }, { path: 'a', secret: true }], 'named twice'],
        [[{ path: '1st', secret: true }], 'not a key name'],
    ])('refuses %j', (leaves, message) => {
        expect(() => structureFromLeaves(leaves)).toThrow(message);
    });
    it('compares keys, nesting and flags, never order', () => {
        expect(sameStructure({ a: 'public', b: { c: 'secret' } }, { b: { c: 'secret' }, a: 'public' })).toBe(true);
        expect(sameStructure({ a: 'public', b: { c: 'secret' } }, { a: 'public', c: 'secret' })).toBe(false);
        expect(sameStructure({ a: 'public' }, { a: 'public' })).toBe(false);
        expect(structureProblem({ a: 'maybe', b: 'secret' })).toContain('must be secret, public or a group');
    });
    it('nests public values by path', () => {
        const values = valuesFromPaths([['url', 'https://x'], ['auth.user', 'ada']]);
        expect(values).toEqual({ url: 'https://x', auth: { user: 'ada' } });
        expect(valueAt(values, 'auth.user')).toBe('ada');
        expect(valueAt(values, 'auth.token')).toBe('');
    });
});
