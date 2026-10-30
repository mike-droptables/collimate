import { describe, it, expect } from 'vitest';
import { SHORTCUT_REGISTRY } from '../registry';
import { SHORTCUT_IDS } from '../ids';
import { SHORTCUT_CONTEXT_ORDER } from '../types';
import { buildForwardedCombos } from '../forwardedCombos';

describe('shortcut registry — fan-out', () => {
    it('registers SCENE_FAN_OUT as scene.fanOut on Alt+v, reachable from a renderer', () => {
        expect(SHORTCUT_IDS.SCENE_FAN_OUT).toBe('scene.fanOut');
        const entry = SHORTCUT_REGISTRY[SHORTCUT_IDS.SCENE_FAN_OUT];
        expect(entry).toBeDefined();
        expect(entry.id).toBe('scene.fanOut');
        expect(entry.default).toBe('Alt+v');
        expect(entry.allowedInRenderer).toBe(true);
        expect(entry.context).toBe('global');
    });

    it('carries no ids, keys or text from the replaced mode', () => {
        // Spelled in pieces so the repository's own leftovers grep stays
        // clean: this test asserts the word is gone, so it must not carry it
        // either. Word-bounded (letters only — `_`, `.`, `-` and case changes
        // like SCENE_X / scene.xDeep still count as a boundary via the second
        // alternative) so words that merely contain it, like "diverge", pass.
        const w = 'd' + 'ive';
        const banned = new RegExp(`(^|[^a-z])${w}([^a-z]|$)|${w}[-_ ]?deep`, 'i');
        expect('SCENE_' + w.toUpperCase() + '_DEEP').toMatch(banned);
        expect('scene.' + w + 'Deep').toMatch(banned);
        expect('Divergence view').not.toMatch(banned);
        for (const [key, value] of Object.entries(SHORTCUT_IDS)) {
            expect(key).not.toMatch(banned);
            expect(value).not.toMatch(banned);
        }
        for (const [key, entry] of Object.entries(SHORTCUT_REGISTRY)) {
            expect(key).not.toMatch(banned);
            expect(entry.id).not.toMatch(banned);
            expect(entry.label).not.toMatch(banned);
            expect(entry.description ?? '').not.toMatch(banned);
        }
    });
});

describe('shortcut registry — context order', () => {
    it('lists every registered context, so the Commands pane lists every shortcut as a row (CP-2)', () => {
        for (const entry of Object.values(SHORTCUT_REGISTRY)) {
            expect(SHORTCUT_CONTEXT_ORDER).toContain(entry.context);
        }
    });
});

describe('shortcut registry — the Commands pane map', () => {
    it('Mod+K keeps its id and toggles the Commands pane; the bare / opens it and is never forwarded (C19)', () => {
        const toggle = SHORTCUT_REGISTRY[SHORTCUT_IDS.GLOBAL_OPEN_PALETTE];
        expect(toggle.id).toBe('global.openPalette');
        expect(toggle.default).toBe('Mod+K');
        expect(toggle.label).toBe('Toggle the Commands pane');
        expect(toggle.allowedInRenderer).toBe(true);
        const slash = SHORTCUT_REGISTRY[SHORTCUT_IDS.GLOBAL_OPEN_COMMANDS];
        expect(slash).toMatchObject({ id: 'global.openCommands', default: '/', context: 'global' });
        expect(slash.allowedInRenderer).toBeFalsy();
    });

    it('the cell search ids are gone and the list keys serve the pane and the cell panels (K02)', () => {
        const ids = Object.values(SHORTCUT_IDS) as string[];
        expect(ids).not.toContain('global.searchCells');
        expect(ids.filter(id => id.startsWith('palette.'))).toEqual([]);
        expect(Object.values(SHORTCUT_REGISTRY).map(e => e.context)).not.toContain('palette');
        for (const id of [SHORTCUT_IDS.LIST_NEXT, SHORTCUT_IDS.LIST_PREV, SHORTCUT_IDS.LIST_RUN_HIGHLIGHTED, SHORTCUT_IDS.LIST_CLOSE]) {
            expect(SHORTCUT_REGISTRY[id].context).toBe('list');
        }
        expect(Object.values(SHORTCUT_REGISTRY).map(e => e.description ?? '').join(' ')).not.toMatch(/right-click/i);
    });

    it('the history view\'s verdict keys and its context are gone (C19)', async () => {
        const { SHORTCUT_CONTEXT_LABEL } = await import('../types');
        expect((Object.values(SHORTCUT_IDS) as string[]).filter(id => id.startsWith('history.'))).toEqual([]);
        expect(Object.keys(SHORTCUT_REGISTRY).filter(id => id.startsWith('history.'))).toEqual([]);
        expect(SHORTCUT_CONTEXT_ORDER as readonly string[]).not.toContain('history');
        expect(Object.keys(SHORTCUT_CONTEXT_LABEL)).not.toContain('history');
    });

    it('SHORTCUT_CONTEXT_LABEL covers SHORTCUT_CONTEXT_ORDER: every listed context has a heading (C19)', async () => {
        const { SHORTCUT_CONTEXT_LABEL } = await import('../types');
        expect(Object.keys(SHORTCUT_CONTEXT_LABEL).sort()).toEqual([...SHORTCUT_CONTEXT_ORDER].sort());
        for (const ctx of SHORTCUT_CONTEXT_ORDER) expect(SHORTCUT_CONTEXT_LABEL[ctx]).toBeTruthy();
        expect(SHORTCUT_CONTEXT_LABEL.list).toBe('Commands pane and cell panels');
    });

    it('the Esc cascade names every rung in the order the scene runs them, the Commands pane before the cell\'s panel (C19)', () => {
        const text = SHORTCUT_REGISTRY[SHORTCUT_IDS.GLOBAL_BACK].description ?? '';
        const rungs = ['pending cell move', 'cell diff', 'version history', 'Commands pane', 'selected cell\'s panel', 'fanned-out stack', 'exit zoom', 'reset focus'];
        const at = rungs.map(rung => text.indexOf(rung));
        expect(at.every(i => i >= 0)).toBe(true);
        expect([...at].sort((a, b) => a - b)).toEqual(at);
    });
});

describe('shortcut registry — Find file', () => {
    it('Mod+P opens the Files panel on the selected cell, forwarded from renderers like Mod+K (P03)', () => {
        const entry = SHORTCUT_REGISTRY[SHORTCUT_IDS.GLOBAL_FIND_FILE];
        expect(entry).toMatchObject({ id: 'global.findFile', default: 'Mod+P', context: 'cell', label: 'Find file', allowedInRenderer: true });
        expect(entry.description).toMatch(/Files panel on the selected cell/);
        const mod = Object.values(SHORTCUT_REGISTRY).filter(e => e.default.toLowerCase() === 'mod+p');
        expect(mod.map(e => e.id)).toEqual(['global.findFile']);
    });

    it('the renderer shell\'s forwarded combos carry Mod+P beside Mod+K (P03)', () => {
        const combos = buildForwardedCombos();
        const modOf = (key: string) => combos.filter(c => c.key.toLowerCase() === key && (c.ctrlKey || c.metaKey) && !c.altKey && !c.shiftKey);
        expect(modOf('k')).toHaveLength(1);
        expect(modOf('p')).toEqual([{ ...modOf('k')[0], key: 'p' }]);
    });
});
