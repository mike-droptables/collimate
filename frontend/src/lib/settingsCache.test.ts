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

    it("carries the server's theme library beside the channel's override, so the first paint has both", () => {
        const themeLibrary = { defaultThemeId: 'user.dusk', userThemes: [] };
        writeBootCache({ theme: { override: 'paper' }, themeLibrary });
        writeBootCache({ keyboard: { overrides: {}, commands: {} } });
        expect(readBootCache().theme).toEqual({ override: 'paper' });
        expect(readBootCache().themeLibrary).toEqual(themeLibrary);
        expect(readBootCache()).not.toHaveProperty('editing');
    });
});
