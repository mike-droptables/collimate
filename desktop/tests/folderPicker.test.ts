import { describe, expect, it } from 'vitest';
import { chosenFolder, folderDialogOptions, parsePickFolder } from '../src/main/folderPicker.js';

/**
 * The folder chooser's main-process half (plan 1105 phase 9): what the
 * page may ask for, the dialog it gets, and the one path that comes back.
 */

describe('parsePickFolder', () => {
  it('keeps the title and a defaultPath, and nothing else', () => {
    expect(parsePickFolder({ title: 'Add a location' })).toEqual({ title: 'Add a location' });
    expect(parsePickFolder({ title: 'Add a location', defaultPath: '/home/me', properties: ['multiSelections'] })).toEqual({
      title: 'Add a location',
      defaultPath: '/home/me',
    });
  });

  it('refuses a payload that is not options, a missing or blank title, and an empty or non-string defaultPath', () => {
    for (const junk of [null, undefined, 'Add', 3, {}, { title: '' }, { title: '   ' }, { title: 7 }, { title: 'x'.repeat(201) }]) {
      expect(() => parsePickFolder(junk)).toThrow();
    }
    expect(() => parsePickFolder({ title: 'Add', defaultPath: '' })).toThrow('defaultPath');
    expect(() => parsePickFolder({ title: 'Add', defaultPath: 3 })).toThrow('defaultPath');
  });
});

describe('folderDialogOptions', () => {
  it('asks for one folder the user may create, never files or several', () => {
    expect(folderDialogOptions({ title: 'Add a location' })).toEqual({
      title: 'Add a location',
      properties: ['openDirectory', 'createDirectory'],
    });
    expect(folderDialogOptions({ title: 'Add a location', defaultPath: '/srv' })).toEqual({
      title: 'Add a location',
      defaultPath: '/srv',
      properties: ['openDirectory', 'createDirectory'],
    });
  });
});

describe('chosenFolder', () => {
  it('is the one chosen path, or null for a cancel or an empty choice', () => {
    expect(chosenFolder({ canceled: false, filePaths: ['/home/me/Channels'] })).toBe('/home/me/Channels');
    expect(chosenFolder({ canceled: true, filePaths: ['/home/me/Channels'] })).toBeNull();
    expect(chosenFolder({ canceled: false, filePaths: [] })).toBeNull();
  });
});
