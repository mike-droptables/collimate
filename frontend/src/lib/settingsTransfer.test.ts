import { describe, expect, it } from 'vitest';
import type { ChannelSettings } from '@/lib/api/settings';
import { parseSettingsImport, serializeSettings, settingsFileName } from './settingsTransfer';

const doc: ChannelSettings = {
    version: 1,
    kind: 'channel-settings',
    keyboard: { overrides: { 'global.findFile': 'Mod+K' }, commands: {} },
    theme: { activeThemeId: null, userThemes: [] },
    editing: { spellcheck: true },
    layout: { defaultStackWidth: 640 },
    remove_zones: { zones: [] },
};

describe('settings JSON transfer (TR-12)', () => {
    it('round-trips the whole doc through the file text (TR-12)', () => {
        const text = serializeSettings(doc);
        expect(text.endsWith('\n')).toBe(true);
        expect(parseSettingsImport(text)).toEqual({ ok: true, doc });
    });

    it('refuses a file whose kind is not channel-settings (TR-12)', () => {
        const result = parseSettingsImport(JSON.stringify({ ...doc, kind: 'something-else' }));
        expect(result.ok).toBe(false);
        expect(result.ok ? '' : result.error).toMatch(/Not a channel settings file/);
        expect(parseSettingsImport(JSON.stringify({ version: 1 })).ok).toBe(false);
    });

    it('refuses text that is not a JSON object (TR-12)', () => {
        expect(parseSettingsImport('{ nope')).toEqual({ ok: false, error: 'The file is not valid JSON.' });
        expect(parseSettingsImport('[1, 2]')).toEqual({ ok: false, error: 'The file is not a settings document.' });
        expect(parseSettingsImport('null')).toEqual({ ok: false, error: 'The file is not a settings document.' });
    });

    it('names the file after the channel, safe on every OS (TR-12)', () => {
        expect(settingsFileName('Research')).toBe('Research-settings.json');
        expect(settingsFileName('a/b:c*?"<>|')).toBe('a-b-c-------settings.json');
        expect(settingsFileName('trailing. ')).toBe('trailing-settings.json');
        expect(settingsFileName('')).toBe('channel-settings.json');
        expect(settingsFileName(undefined)).toBe('channel-settings.json');
    });
});
