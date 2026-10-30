// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readBootCache, writeBootCache } from './settingsCache';

const LS_KEY = 'channel-settings.cache.v1';

describe('writeBootCache', () => {
    afterEach(() => {
        vi.restoreAllMocks();
        localStorage.clear();
    });

    it('merges several sections with a single localStorage write', () => {
        const theme = { active: 'dark' } as never;
        const keyboard = { overrides: { save: ['mod+s'] } } as never;
        localStorage.setItem(LS_KEY, JSON.stringify({ theme }));
        const setItem = vi.spyOn(Storage.prototype, 'setItem');
        writeBootCache({ keyboard });
        expect(setItem).toHaveBeenCalledTimes(1);
        expect(readBootCache()).toEqual({ theme, keyboard });
    });

    it('carries the editing section beside theme and keyboard, so the first paint has the spellcheck flag', () => {
        writeBootCache({ theme: { active: 'paper' } as never, editing: { spellcheck: false } });
        writeBootCache({ keyboard: { overrides: {}, commands: {} } });
        expect(readBootCache().editing).toEqual({ spellcheck: false });
    });
});
