import { describe, it, expect, beforeEach, vi } from 'vitest';

// Reset the store + localStorage before every test. We re-import
// the module to force the boot-cache read at module eval to re-read
// an empty localStorage each run. Overrides persist per channel via
// putChannelSettings — with no active channel in tests, only the
// localStorage boot cache (channel-settings.cache.v1) is written.
const CACHE_KEY = 'channel-settings.cache.v1';

function cachedOverrides(storage: Map<string, string>): Record<string, string> | undefined {
    const raw = storage.get(CACHE_KEY);
    return raw ? JSON.parse(raw).keyboard?.overrides : undefined;
}

function cachedKeyboard(storage: Map<string, string>): { overrides: Record<string, string>; commands: Record<string, string> } | undefined {
    const raw = storage.get(CACHE_KEY);
    return raw ? JSON.parse(raw).keyboard : undefined;
}

async function freshStore() {
    vi.resetModules();
    // Provide a localStorage shim that tests can inspect/preload.
    const storage = new Map<string, string>();
    vi.stubGlobal('localStorage', {
        getItem: (k: string) => storage.get(k) ?? null,
        setItem: (k: string, v: string) => { storage.set(k, v); },
        removeItem: (k: string) => { storage.delete(k); },
        clear: () => storage.clear(),
    });
    const mod = await import('../store');
    return { store: mod.useKeybindingsStore, storage };
}

beforeEach(() => vi.unstubAllGlobals());

describe('keybindings store', () => {
    it('resolveString returns the registry default when no override', async () => {
        const { store } = await freshStore();
        expect(store.getState().resolveString('global.openPalette')).toBe('Mod+k');
    });

    it('resolve returns a parsed KeyCombo for known ids', async () => {
        const { store } = await freshStore();
        const combo = store.getState().resolve('editor.save');
        expect(combo).toEqual({ mod: true, key: 's' });
    });

    it('resolve returns null for unknown ids', async () => {
        const { store } = await freshStore();
        expect(store.getState().resolve('nope.does.not.exist')).toBeNull();
    });

    it('setOverride replaces the default and persists to the boot cache', async () => {
        const { store, storage } = await freshStore();
        store.getState().setOverride('global.openPalette', 'Mod+Shift+P');
        expect(store.getState().resolveString('global.openPalette')).toBe('Mod+Shift+p');
        expect(cachedOverrides(storage)?.['global.openPalette']).toBe('Mod+Shift+p');
    });

    it('setOverride with null falls back to the default', async () => {
        const { store } = await freshStore();
        store.getState().setOverride('global.openPalette', 'Mod+Shift+P');
        expect(store.getState().resolveString('global.openPalette')).toBe('Mod+Shift+p');
        store.getState().setOverride('global.openPalette', null);
        expect(store.getState().resolveString('global.openPalette')).toBe('Mod+k');
    });

    it('setOverride silently ignores unknown ids', async () => {
        const { store, storage } = await freshStore();
        store.getState().setOverride('nope.fake', 'Mod+K');
        expect(storage.size).toBe(0);
    });

    it('setOverride silently drops unparseable combo strings', async () => {
        const { store } = await freshStore();
        const before = store.getState().resolveString('global.openPalette');
        // Modifiers with no key — parseCombo returns null.
        store.getState().setOverride('global.openPalette', 'Shift+Alt');
        expect(store.getState().resolveString('global.openPalette')).toBe(before);
    });

    it('resetAll clears every override', async () => {
        const { store, storage } = await freshStore();
        store.getState().setOverride('global.openPalette', 'Mod+R');
        store.getState().setOverride('editor.save', 'Mod+Shift+D');
        store.getState().resetAll();
        expect(store.getState().overrides).toEqual({});
        expect(cachedOverrides(storage)).toEqual({});
    });

    it('adoptOverrides replaces state without persisting (server is the source)', async () => {
        const { store, storage } = await freshStore();
        store.getState().adoptOverrides({
            'editor.save': 'Mod+r',        // valid, known
            'nope.fake': 'Mod+K',         // unknown id — dropped
        });
        expect(store.getState().resolveString('editor.save')).toBe('Mod+r');
        expect(store.getState().overrides['nope.fake']).toBeUndefined();
        expect(storage.get(CACHE_KEY)).toBeUndefined();
    });

    it('boot cache seeds the initial overrides', async () => {
        vi.resetModules();
        const storage = new Map<string, string>();
        storage.set(CACHE_KEY, JSON.stringify({
            keyboard: { overrides: { 'editor.save': 'Mod+Shift+r', 'nav.cellMoveRight':'x' } },
        }));
        vi.stubGlobal('localStorage', {
            getItem: (k: string) => storage.get(k) ?? null,
            setItem: (k: string, v: string) => { storage.set(k, v); },
            removeItem: (k: string) => { storage.delete(k); },
            clear: () => storage.clear(),
        });
        const mod = await import('../store');
        expect(mod.useKeybindingsStore.getState().resolveString('editor.save')).toBe('Mod+Shift+r');
        expect(mod.useKeybindingsStore.getState().resolveString('nav.cellMoveRight')).toBe('Ctrl+Shift+ArrowRight');
    });
});


describe('keybindings store command bindings', () => {
    it('setCommandBinding canonicalises the combo and persists it beside the overrides (CP-2)', async () => {
        const { store, storage } = await freshStore();
        store.getState().setOverride('editor.save', 'Mod+Shift+D');
        store.getState().setCommandBinding('log', 'shift+alt+L');
        expect(store.getState().commands).toEqual({ log: 'Alt+Shift+l' });
        store.getState().setCommandBinding('goto:a', { key: 'g', mod: true });
        expect(cachedKeyboard(storage)).toEqual({
            overrides: { 'editor.save': 'Mod+Shift+d' },
            commands: { log: 'Alt+Shift+l', 'goto:a': 'Mod+g' },
        });
        // An override write carries the command bindings too (the section is
        // written whole).
        store.getState().setOverride('editor.save', null);
        expect(cachedKeyboard(storage)).toEqual({ overrides: {}, commands: { log: 'Alt+Shift+l', 'goto:a': 'Mod+g' } });
    });

    it('setCommandBinding with null unbinds; an unparseable combo or a bad key changes nothing (CP-2)', async () => {
        const { store, storage } = await freshStore();
        store.getState().setCommandBinding('log', 'Alt+l');
        store.getState().setCommandBinding('log', 'Shift+Alt');
        expect(store.getState().commands).toEqual({ log: 'Alt+l' });
        store.getState().setCommandBinding('', 'Alt+x');
        store.getState().setCommandBinding('k'.repeat(201), 'Alt+x');
        expect(store.getState().commands).toEqual({ log: 'Alt+l' });
        store.getState().setCommandBinding('k'.repeat(200), 'Alt+x');
        expect(Object.keys(store.getState().commands)).toHaveLength(2);
        store.getState().setCommandBinding('log', null);
        store.getState().setCommandBinding('k'.repeat(200), null);
        expect(store.getState().commands).toEqual({});
        expect(cachedKeyboard(storage)?.commands).toEqual({});
    });

    it('resetAll clears the command bindings with the overrides (CP-2)', async () => {
        const { store, storage } = await freshStore();
        store.getState().setOverride('editor.save', 'Mod+Shift+D');
        store.getState().setCommandBinding('log', 'Alt+l');
        store.getState().resetAll();
        expect(store.getState()).toMatchObject({ overrides: {}, commands: {} });
        expect(cachedKeyboard(storage)).toEqual({ overrides: {}, commands: {} });
    });

    it('adoptCommandBindings sanitises without persisting (CP-2)', async () => {
        const { store, storage } = await freshStore();
        store.getState().adoptCommandBindings({
            log: 'alt+L', '': 'Alt+x', ['k'.repeat(201)]: 'Alt+y', bad: 'Shift+Alt', num: 3,
        } as unknown as Record<string, string>);
        expect(store.getState().commands).toEqual({ log: 'Alt+l' });
        expect(storage.get(CACHE_KEY)).toBeUndefined();
    });

    it('the boot cache seeds the command bindings (CP-2)', async () => {
        vi.resetModules();
        const storage = new Map<string, string>();
        storage.set(CACHE_KEY, JSON.stringify({ keyboard: { overrides: {}, commands: { log: 'Alt+l', bad: 7 } } }));
        vi.stubGlobal('localStorage', {
            getItem: (k: string) => storage.get(k) ?? null,
            setItem: (k: string, v: string) => { storage.set(k, v); },
            removeItem: (k: string) => { storage.delete(k); },
            clear: () => storage.clear(),
        });
        const mod = await import('../store');
        expect(mod.useKeybindingsStore.getState().commands).toEqual({ log: 'Alt+l' });
    });

    it('capturing is session state, set and cleared (CP-2)', async () => {
        const { store, storage } = await freshStore();
        expect(store.getState().capturing).toBeNull();
        store.getState().setCapturing('log');
        expect(store.getState().capturing).toBe('log');
        store.getState().setCapturing(null);
        expect(store.getState().capturing).toBeNull();
        expect(storage.size).toBe(0);
    });
});

describe('cell move requires a modifier __F0913',()=>{
 it('rejects bare-key writes/adoption while keeping general shortcuts remappable',async()=>{
  const {store}=await freshStore();const before=store.getState().resolveString('nav.cellMoveRight');
  store.getState().setOverride('nav.cellMoveRight','x');expect(store.getState().resolveString('nav.cellMoveRight')).toBe(before);
  store.getState().adoptOverrides({'nav.cellMoveRight':'x','editor.save':'x'});
  expect(store.getState().resolveString('nav.cellMoveRight')).toBe(before);
  expect(store.getState().resolveString('editor.save')).toBe('x');
  store.getState().setOverride('nav.cellMoveRight','Meta+ArrowRight');
  expect(store.getState().resolveString('nav.cellMoveRight')).toBe('Meta+ArrowRight');
 });
});
