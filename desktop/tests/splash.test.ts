import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it, vi } from 'vitest';
import { parseAction, Splash } from '../src/main/splash.js';
import { DEFAULT_WINDOW_TITLE, pinWindowTitle, type TitlePinnable } from '../src/main/window.js';

/**
 * The splash action parser: everything the splash page can send arrives as
 * untrusted IPC payload, and parseAction is the only gate. The shapes are
 * tiny; what matters is that junk becomes null, not a half-formed action.
 *
 * Plus the one thing the splash does to its window that a page can undo —
 * see pinWindowTitle at the bottom.
 */

describe('parseAction', () => {
  it.each([
    [{ type: 'retry' }, { type: 'retry' }],
    [{ type: 'quit' }, { type: 'quit' }],
    [{ type: 'spawn' }, { type: 'spawn' }],
    [{ type: 'connect', url: 'http://box:5001' }, { type: 'connect', url: 'http://box:5001' }],
    [
      { type: 'connect', url: 'http://box:5001', serverId: 'srv-1' },
      { type: 'connect', url: 'http://box:5001', serverId: 'srv-1' },
    ],
  ])('parses %j', (payload, expected) => {
    expect(parseAction(payload)).toEqual(expected);
  });

  it('drops extra keys rather than rejecting them', () => {
    expect(parseAction({ type: 'spawn', extra: 1 })).toEqual({ type: 'spawn' });
  });

  it('carries a connect token only when it is a non-empty string', () => {
    expect(parseAction({ type: 'connect', url: 'http://b', token: 't' })).toEqual({
      type: 'connect',
      url: 'http://b',
      token: 't',
    });
    expect(parseAction({ type: 'connect', url: 'http://b', token: '' })).toEqual({
      type: 'connect',
      url: 'http://b',
    });
    expect(parseAction({ type: 'connect', url: 'http://b', token: 42 })).toEqual({
      type: 'connect',
      url: 'http://b',
    });
  });

  it('carries a saved server id only when it is a non-empty string (MS-02)', () => {
    expect(parseAction({ type: 'connect', url: 'http://b', serverId: '' })).toEqual({ type: 'connect', url: 'http://b' });
    expect(parseAction({ type: 'connect', url: 'http://b', serverId: 7 })).toEqual({ type: 'connect', url: 'http://b' });
  });

  it.each([
    [null],
    ['spawn'],
    [{ type: 'unknown' }],
    [{ type: 'cancelSwitch' }],
    [{ type: 'connect' }],
    [{ type: 'connect', url: 42 }],
  ])('rejects %j', (payload) => {
    expect(parseAction(payload)).toBeNull();
  });
});

/**
 * A stand-in BrowserWindow that counts how often the splash raises it; the
 * rest of its surface is just enough for Splash.show() to wire it up.
 */
const electronFake = vi.hoisted(() => {
  class FakeBrowserWindow {
    static instances: FakeBrowserWindow[] = [];
    shows = 0;
    focuses = 0;
    height = 300;
    webContents = { on: () => undefined, send: () => undefined };
    constructor() {
      FakeBrowserWindow.instances.push(this);
    }
    isDestroyed(): boolean {
      return false;
    }
    show(): void {
      this.shows += 1;
    }
    focus(): void {
      this.focuses += 1;
    }
    once(): void {}
    on(): void {}
    getContentSize(): [number, number] {
      return [520, this.height];
    }
    setContentSize(_width: number, height: number): void {
      this.height = height;
    }
    center(): void {}
    loadFile(): Promise<void> {
      return Promise.resolve();
    }
    destroy(): void {}
  }
  return { FakeBrowserWindow };
});

vi.mock('electron', () => ({
  BrowserWindow: electronFake.FakeBrowserWindow,
  ipcMain: { on: () => undefined, off: () => undefined },
  shell: {},
}));

describe('Splash.setView', () => {
  it('does not re-focus the open splash on every status tick, only on a view change', () => {
    electronFake.FakeBrowserWindow.instances.length = 0;
    const splash = new Splash({ onAction: () => undefined });
    splash.show();
    const window = electronFake.FakeBrowserWindow.instances[0];
    expect(window).toBeDefined();
    if (!window) return;

    splash.status('Waiting for server…');
    splash.status('Waiting for server…', '2s');
    splash.status('Waiting for server…', '3s');
    expect(electronFake.FakeBrowserWindow.instances).toHaveLength(1);
    expect(window.focuses).toBe(0);

    splash.setView({ kind: 'failure', title: 'Server exited', message: 'Exit code 1', tail: [] });
    expect(window.focuses).toBe(1);

    splash.close();
  });
});

/**
 * The window title the splash (and the main window) hold on to.
 *
 * Splash.show() constructs a real BrowserWindow, which no unit test can do,
 * so the seam is the helper itself: whether the page-title-updated handler
 * gets installed is the entire decision, and it is the same decision for
 * both windows. What it defends against is real — splash.html declares
 * <title>Collimate</title>, so a dev run whose splash did not prevent the
 * default would introduce itself to the taskbar as the installed app.
 */
describe('pinWindowTitle', () => {
  function fakeWindow(): {
    window: TitlePinnable;
    listeners: ((event: { preventDefault: () => void }) => void)[];
  } {
    const listeners: ((event: { preventDefault: () => void }) => void)[] = [];
    return {
      listeners,
      window: {
        webContents: {
          on: (_event, listener) => {
            listeners.push(listener);
          },
        },
      },
    };
  }

  it('refuses the page’s title when the window has a profile title', () => {
    const { window, listeners } = fakeWindow();

    expect(pinWindowTitle(window, 'Collimate Dev')).toBe(true);
    expect(listeners).toHaveLength(1);

    let prevented = 0;
    listeners[0]?.({
      preventDefault: () => {
        prevented += 1;
      },
    });
    expect(prevented).toBe(1);
  });

  it('installs nothing when the title is the one every page already declares', () => {
    // The stable app's windows must behave byte-for-byte as they did before
    // profiles existed — no handler, no interception, nothing to regress.
    const { window, listeners } = fakeWindow();

    expect(pinWindowTitle(window, DEFAULT_WINDOW_TITLE)).toBe(false);
    expect(listeners).toEqual([]);
  });
});

/**
 * The splash document's connect view, read as text: the saved-server
 * buttons name the server by id (main resolves its token), and the local
 * choice is worded as what it does.
 */
describe('splash.html connect view (MS-02)', () => {
  const html = readFileSync(fileURLToPath(new URL('../src/splash/splash.html', import.meta.url)), 'utf8');

  it('offers "Start local server" and no Cancel (MS-02)', () => {
    expect(html).toContain('>Start local server</button>');
    expect(html).not.toContain('Use local server instead');
    expect(html).not.toContain('cancelSwitch');
    expect(html).not.toContain('connect-cancel');
  });

  it('sends a saved server by id, never with a token (MS-02)', () => {
    expect(html).toContain('send({ type: "connect", url: server.url, serverId: server.id });');
  });
});
