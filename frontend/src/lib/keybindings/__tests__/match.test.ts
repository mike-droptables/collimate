import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { comboHasModifier, isTypingTarget, matchCombo, normalizeEventKey } from '../match';
import { parseCombo } from '../parser';

// The matchCombo function only reads 5 fields off its event
// argument — we fabricate minimal event-like objects here rather
// than pulling jsdom in for the whole test suite. Platform is
// controlled via vi.stubGlobal on `navigator` so tests don't rely
// on the host OS.

type FakeKeyEvent = {
    key: string;
    metaKey?: boolean;
    ctrlKey?: boolean;
    altKey?: boolean;
    shiftKey?: boolean;
};

function evt(e: FakeKeyEvent): KeyboardEvent {
    // Cast through unknown — matchCombo only reads key + modifier
    // flags, and TypeScript understands that. Avoids the jsdom
    // dependency.
    return e as unknown as KeyboardEvent;
}

function setPlatform(value: string) {
    vi.stubGlobal('navigator', { platform: value, userAgent: '' });
}
function restorePlatform() {
    vi.unstubAllGlobals();
}

describe('normalizeEventKey', () => {
    it('lowercases single letters', () => {
        expect(normalizeEventKey('K')).toBe('k');
        expect(normalizeEventKey('a')).toBe('a');
    });
    it('passes named keys verbatim', () => {
        expect(normalizeEventKey('Enter')).toBe('Enter');
        expect(normalizeEventKey('ArrowLeft')).toBe('ArrowLeft');
    });
    it('handles empty', () => {
        expect(normalizeEventKey('')).toBe('');
    });
});

describe('matchCombo — literal modifiers', () => {
    beforeEach(() => setPlatform('Linux x86_64'));
    afterEach(() => restorePlatform());

    it('matches bare key only when no modifiers held', () => {
        const combo = parseCombo('Escape')!;
        expect(matchCombo(evt({ key: 'Escape' }), combo)).toBe(true);
        expect(matchCombo(evt({ key: 'Escape', shiftKey: true }), combo)).toBe(false);
    });

    it('matches Ctrl+K only with ctrlKey true (not metaKey)', () => {
        const combo = parseCombo('Ctrl+K')!;
        expect(matchCombo(evt({ key: 'k', ctrlKey: true }), combo)).toBe(true);
        expect(matchCombo(evt({ key: 'k', metaKey: true }), combo)).toBe(false);
    });

    it('requires exact shift match', () => {
        const enter = parseCombo('Enter')!;
        const shiftEnter = parseCombo('Shift+Enter')!;
        expect(matchCombo(evt({ key: 'Enter' }), enter)).toBe(true);
        expect(matchCombo(evt({ key: 'Enter', shiftKey: true }), enter)).toBe(false);
        expect(matchCombo(evt({ key: 'Enter', shiftKey: true }), shiftEnter)).toBe(true);
        expect(matchCombo(evt({ key: 'Enter' }), shiftEnter)).toBe(false);
    });

    it('does not fire when an extra unused modifier is pressed', () => {
        const combo = parseCombo('Ctrl+K')!;
        expect(matchCombo(evt({ key: 'k', ctrlKey: true, metaKey: true }), combo)).toBe(false);
        expect(matchCombo(evt({ key: 'k', ctrlKey: true, altKey: true }), combo)).toBe(false);
    });
});

describe('matchCombo — Mod on Linux / Windows', () => {
    beforeEach(() => setPlatform('Linux x86_64'));
    afterEach(() => restorePlatform());

    it('Mod maps to Ctrl on non-Mac', () => {
        const combo = parseCombo('Mod+K')!;
        expect(matchCombo(evt({ key: 'k', ctrlKey: true }), combo)).toBe(true);
        expect(matchCombo(evt({ key: 'k', metaKey: true }), combo)).toBe(false);
    });

    it('Mod with Shift on Linux requires Ctrl+Shift', () => {
        const combo = parseCombo('Mod+Shift+D')!;
        expect(matchCombo(evt({ key: 'd', ctrlKey: true, shiftKey: true }), combo)).toBe(true);
        expect(matchCombo(evt({ key: 'd', ctrlKey: true }), combo)).toBe(false);
    });
});

describe('matchCombo — Mod on Mac', () => {
    beforeEach(() => setPlatform('MacIntel'));
    afterEach(() => restorePlatform());

    it('Mod maps to Meta on Mac', () => {
        const combo = parseCombo('Mod+K')!;
        expect(matchCombo(evt({ key: 'k', metaKey: true }), combo)).toBe(true);
        expect(matchCombo(evt({ key: 'k', ctrlKey: true }), combo)).toBe(false);
    });
});


describe('macOS physical Option defaults __F0479',()=>{
 afterEach(()=>restorePlatform());
 it('matches Option letters and punctuation while other platforms keep logical keys',()=>{
  setPlatform('MacIntel');
  expect(matchCombo(({...evt({key:'å',altKey:true}),code:'KeyA'} as KeyboardEvent),{key:'a',alt:true})).toBe(true);
  expect(matchCombo(({...evt({key:'÷',altKey:true}),code:'Slash'} as KeyboardEvent),{key:'/',alt:true})).toBe(true);
  setPlatform('Linux');
  expect(matchCombo(({...evt({key:'å',altKey:true}),code:'KeyA'} as KeyboardEvent),{key:'a',alt:true})).toBe(false);
 });
});

describe('comboHasModifier', () => {
    it('Mod, Ctrl, Meta and Alt take a combo out of typing; Shift alone does not (CP-2)', () => {
        for (const combo of ['Mod+k', 'Ctrl+k', 'Meta+k', 'Alt+k', 'Alt+Shift+k']) expect(comboHasModifier(parseCombo(combo)!)).toBe(true);
        for (const combo of ['k', 'Shift+k', 'Enter', '/']) expect(comboHasModifier(parseCombo(combo)!)).toBe(false);
    });
});

describe('isTypingTarget', () => {
    it('is false off the DOM: a target that is no element (CP-2)', () => {
        const e = { composedPath: () => [], target: null } as unknown as Event;
        expect(isTypingTarget(e)).toBe(false);
    });
});
