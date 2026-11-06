// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { saveJsonFile } from './settingsTransfer';

type PickerWindow = { showSaveFilePicker?: unknown };

afterEach(() => {
    delete (window as PickerWindow).showSaveFilePicker;
    vi.restoreAllMocks();
});

describe('saving the settings file', () => {
    it('writes the text into the file the save dialog picked', async () => {
        const written: Blob[] = [];
        const close = vi.fn(async () => {});
        const picker = vi.fn(async () => ({ createWritable: async () => ({ write: async (data: Blob) => { written.push(data); }, close }) }));
        (window as PickerWindow).showSaveFilePicker = picker;
        expect(await saveJsonFile('a-settings.json', '{}\n')).toBe(true);
        expect(picker).toHaveBeenCalledWith(expect.objectContaining({ suggestedName: 'a-settings.json' }));
        expect(await written[0].text()).toBe('{}\n');
        expect(close).toHaveBeenCalledOnce();
    });

    it('is false when the dialog was cancelled, and rethrows any other failure', async () => {
        (window as PickerWindow).showSaveFilePicker = vi.fn(async () => { throw new DOMException('closed', 'AbortError'); });
        expect(await saveJsonFile('a.json', '{}')).toBe(false);
        (window as PickerWindow).showSaveFilePicker = vi.fn(async () => { throw new DOMException('denied', 'SecurityError'); });
        await expect(saveJsonFile('a.json', '{}')).rejects.toThrow('denied');
    });

    it('falls back to a download where there is no save dialog', async () => {
        const createObjectURL = vi.fn(() => 'blob:settings');
        Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
        const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
        expect(await saveJsonFile('a-settings.json', '{}')).toBe(true);
        expect(click).toHaveBeenCalledOnce();
        const anchor = click.mock.contexts[0] as HTMLAnchorElement;
        expect(anchor.download).toBe('a-settings.json');
        expect(anchor.isConnected).toBe(false);
    });
});
