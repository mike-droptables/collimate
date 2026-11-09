import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { BrowserWindow } from 'electron';
import {
  commitEditingPrefs,
  defaultSettings,
  editingPrefsOf,
  installEditingPrefsPush,
  readSettings,
  writeSettings,
  type DesktopSettings,
} from '../src/main/connect.js';
import { IPC, type EditingPrefs } from '../src/shared/bridge.js';

/**
 * The main process's editing preferences, end to end over a real
 * settings.json in a scratch directory: what the page is answered (the
 * window bootstrap and every push read editingPrefsOf(settings)), what the
 * `collimate:editing-prefs-set` handler saves and pushes (index.ts hands
 * commitEditingPrefs its saveSettings and its live-window send), and the
 * after-every-load push installEditingPrefsPush adds to the app window.
 */

let dir: string;
const file = (): string => path.join(dir, 'settings.json');
const onDisk = (): Record<string, unknown> => JSON.parse(readFileSync(file(), 'utf8')) as Record<string, unknown>;

beforeEach(() => {
  dir = mkdtempSync(path.join(os.tmpdir(), 'collimate-editing-prefs-'));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

/**
 * The handler as index.ts wires it: `settings` is the process's copy,
 * saveSettings replaces it and writes the file, push is the window send.
 */
function handlerRig(initial: DesktopSettings) {
  let settings = initial;
  const save = vi.fn((next: DesktopSettings) => {
    settings = next;
    writeSettings(dir, next);
  });
  const push = vi.fn<(prefs: EditingPrefs) => void>();
  return {
    set: (payload: unknown) => commitEditingPrefs(settings, payload, { save, push }),
    current: () => settings,
    save,
    push,
  };
}

function windowFixture() {
  const contentsListeners = new Map<string, () => void>();
  let onClosed: (() => void) | null = null;
  let destroyed = false;
  const webContents = {
    send: vi.fn(),
    isDestroyed: () => destroyed,
    on: (name: string, handler: () => void) => contentsListeners.set(name, handler),
    removeListener: (name: string, handler: () => void) => {
      if (contentsListeners.get(name) === handler) contentsListeners.delete(name);
    },
  };
  const window = {
    webContents,
    once: (name: string, handler: () => void) => {
      if (name === 'closed') onClosed = handler;
    },
  };
  return {
    window: window as unknown as BrowserWindow,
    webContents,
    load: () => contentsListeners.get('did-finish-load')?.(),
    listening: () => contentsListeners.has('did-finish-load'),
    destroy: () => { destroyed = true; },
    close: () => onClosed?.(),
  };
}

describe('the editing preferences the page is answered', () => {
  it('are both on with no settings file', () => {
    expect(editingPrefsOf(readSettings(dir))).toEqual({ spellcheck: true, statusOnRun: true });
  });

  it('turn spellcheck off for spellcheck: false only', () => {
    writeFileSync(file(), JSON.stringify({ spellcheck: false }));
    expect(editingPrefsOf(readSettings(dir))).toEqual({ spellcheck: false, statusOnRun: true });
  });

  it('turn status-on-run off for openStatusOnRun: false only', () => {
    writeFileSync(file(), JSON.stringify({ openStatusOnRun: false }));
    expect(editingPrefsOf(readSettings(dir))).toEqual({ spellcheck: true, statusOnRun: false });
  });

  it('treat a junk value, or a junk file, as on', () => {
    for (const junk of [null, 0, '', 'false', 'no', [], {}, 1]) {
      writeFileSync(file(), JSON.stringify({ spellcheck: junk, openStatusOnRun: junk }));
      expect(editingPrefsOf(readSettings(dir)), JSON.stringify(junk)).toEqual({ spellcheck: true, statusOnRun: true });
    }
    writeFileSync(file(), '{ not json');
    expect(editingPrefsOf(readSettings(dir))).toEqual({ spellcheck: true, statusOnRun: true });
  });
});

describe('collimate:editing-prefs-set', () => {
  it('persists false under the profile keys, leaving every other key as it was', () => {
    writeSettings(dir, { ...defaultSettings(), keepLocalRunning: true, localPort: 41_234 });
    const before = onDisk();
    const rig = handlerRig(readSettings(dir));

    expect(rig.set({ statusOnRun: false })).toEqual({ spellcheck: true, statusOnRun: false });
    expect(onDisk()).toEqual({ ...before, openStatusOnRun: false });
    expect(rig.set({ spellcheck: false })).toEqual({ spellcheck: false, statusOnRun: false });
    expect(onDisk()).toEqual({ ...before, spellcheck: false, openStatusOnRun: false });
    // Only the profile key names reach the file: never the bridge's statusOnRun.
    expect(onDisk()).not.toHaveProperty('statusOnRun');
    // And what was saved is what the next launch reads.
    expect(editingPrefsOf(readSettings(dir))).toEqual({ spellcheck: false, statusOnRun: false });
  });

  it('writes true back as true, which the next launch reads as on', () => {
    writeFileSync(file(), JSON.stringify({ spellcheck: false, openStatusOnRun: false }));
    const rig = handlerRig(readSettings(dir));

    expect(rig.set({ spellcheck: true, statusOnRun: true })).toEqual({ spellcheck: true, statusOnRun: true });
    expect(onDisk()).toMatchObject({ spellcheck: true, openStatusOnRun: true });
    expect(editingPrefsOf(readSettings(dir))).toEqual({ spellcheck: true, statusOnRun: true });
  });

  it('saves nothing when the patch changes nothing, and still answers and pushes the prefs', () => {
    writeFileSync(file(), JSON.stringify({ spellcheck: false }));
    const mtime = statSync(file()).mtimeMs;
    const rig = handlerRig(readSettings(dir));

    for (const patch of [{}, { spellcheck: false }, { statusOnRun: true }]) {
      expect(rig.set(patch)).toEqual({ spellcheck: false, statusOnRun: true });
    }
    expect(rig.save).not.toHaveBeenCalled();
    expect(readFileSync(file(), 'utf8')).toBe(JSON.stringify({ spellcheck: false }));
    expect(statSync(file()).mtimeMs).toBe(mtime);
    // The push is not deduplicated: every set tells every listener.
    expect(rig.push).toHaveBeenCalledTimes(3);
    expect(rig.push).toHaveBeenLastCalledWith({ spellcheck: false, statusOnRun: true });
  });

  it('rejects a malformed payload without touching the file, the settings or the page', () => {
    writeFileSync(file(), JSON.stringify({ spellcheck: false, keepLocalRunning: true }));
    const raw = readFileSync(file(), 'utf8');
    const rig = handlerRig(readSettings(dir));
    const settings = rig.current();

    const malformed: unknown[] = [
      null, undefined, true, 'spellcheck', 1, [], [false],
      { spellcheck: 'false' }, { statusOnRun: 0 }, { spellcheck: null },
      { openStatusOnRun: false }, { spellcheck: true, extra: 1 },
    ];
    for (const payload of malformed) {
      expect(() => rig.set(payload), String(JSON.stringify(payload))).toThrow(/setEditingPrefs/);
    }
    expect(rig.save).not.toHaveBeenCalled();
    expect(rig.push).not.toHaveBeenCalled();
    expect(rig.current()).toBe(settings);
    expect(readFileSync(file(), 'utf8')).toBe(raw);
  });

  it('a payload mixing a good key with a bad one saves neither', () => {
    const rig = handlerRig(readSettings(dir));
    expect(() => rig.set({ spellcheck: false, statusOnRun: 'off' })).toThrow(/statusOnRun is not a boolean/);
    expect(rig.save).not.toHaveBeenCalled();
    expect(() => statSync(file())).toThrow();
  });

  it('pushes the new prefs after the save, once per set', () => {
    const order: string[] = [];
    let settings = readSettings(dir);
    const prefs = commitEditingPrefs(settings, { spellcheck: false }, {
      save: (next) => {
        order.push('save');
        settings = next;
      },
      push: (pushed) => order.push(`push ${JSON.stringify(pushed)}`),
    });
    expect(prefs).toEqual({ spellcheck: false, statusOnRun: true });
    expect(order).toEqual(['save', 'push {"spellcheck":false,"statusOnRun":true}']);
    expect(settings.spellcheck).toBe(false);
  });
});

describe('installEditingPrefsPush', () => {
  it('sends the current prefs on collimate:editing-prefs after every main-frame load', () => {
    const fixture = windowFixture();
    const rig = handlerRig(readSettings(dir));
    installEditingPrefsPush(fixture.window, () => editingPrefsOf(rig.current()));

    expect(fixture.webContents.send).not.toHaveBeenCalled();
    fixture.load();
    expect(fixture.webContents.send).toHaveBeenLastCalledWith(IPC.editingPrefs, { spellcheck: true, statusOnRun: true });
    // A set changes what the next load pushes: a reloaded page's argv
    // bootstrap still carries the values the window opened with.
    rig.set({ statusOnRun: false });
    fixture.load();
    expect(fixture.webContents.send).toHaveBeenLastCalledWith(IPC.editingPrefs, { spellcheck: true, statusOnRun: false });
    expect(fixture.webContents.send).toHaveBeenCalledTimes(2);
    expect(IPC.editingPrefs).toBe('collimate:editing-prefs');
  });

  it('sends nothing to destroyed contents', () => {
    const fixture = windowFixture();
    installEditingPrefsPush(fixture.window, () => ({ spellcheck: true, statusOnRun: true }));
    fixture.destroy();
    fixture.load();
    expect(fixture.webContents.send).not.toHaveBeenCalled();
  });

  it('stops listening when the window closes', () => {
    const fixture = windowFixture();
    installEditingPrefsPush(fixture.window, () => ({ spellcheck: true, statusOnRun: true }));
    expect(fixture.listening()).toBe(true);
    fixture.close();
    expect(fixture.listening()).toBe(false);
  });
});
