import { describe, expect, it, vi } from 'vitest';
import { chooseFolder } from '../src/main/folderPicker.js';

describe('the native folder dialog', () => {
  it('asks for one folder and answers its path', async () => {
    const show = vi.fn(async () => ({ canceled: false, filePaths: ['/home/me/sdk'] }));
    expect(await chooseFolder(show)).toBe('/home/me/sdk');
    expect(show).toHaveBeenCalledWith({ title: 'Choose a folder', properties: ['openDirectory'] });
  });

  it('answers null when cancelled or nothing was picked', async () => {
    expect(await chooseFolder(async () => ({ canceled: true, filePaths: ['/ignored'] }))).toBeNull();
    expect(await chooseFolder(async () => ({ canceled: false, filePaths: [] }))).toBeNull();
  });
});
