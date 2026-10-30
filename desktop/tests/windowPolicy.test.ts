import { describe, expect, it, vi } from 'vitest';

interface FakeOptions {
  webPreferences?: { spellcheck?: boolean };
  x?: number;
  y?: number;
  width?: number;
  height?: number;
}

const fake = vi.hoisted(() => {
  const options: FakeOptions[] = [];
  const loaded: string[] = [];
  class BrowserWindow {
    /** webContents events the window's installers subscribed to, in order. */
    readonly contentsEvents: string[] = [];
    readonly onceHandlers = new Map<string, () => void>();
    /** show / maximize calls, in order. */
    readonly calls: string[] = [];
    webContents = {
      on: (event: string) => { this.contentsEvents.push(event); },
      setWindowOpenHandler: () => undefined,
    };
    constructor(value: FakeOptions) { options.push(value); }
    on(): void {}
    once(event: string, handler: () => void): void { this.onceHandlers.set(event, handler); }
    show(): void { this.calls.push('show'); }
    maximize(): void { this.calls.push('maximize'); }
    loadURL(url: string): Promise<void> { loaded.push(url); return Promise.resolve(); }
    isDestroyed(): boolean { return false; }
    setTitle(): void {}
  }
  return { options, loaded, BrowserWindow };
});
vi.mock('electron', () => ({ BrowserWindow: fake.BrowserWindow, Menu: {}, shell: { openExternal: vi.fn() } }));
import type { BrowserWindow } from 'electron';
import { installContextMenu } from '../src/main/contextMenu.js';
import { createMainWindow } from '../src/main/window.js';

const SERVER = { id: 'local', kind: 'local', name: 'Local', url: 'http://127.0.0.1:1234', port: 1234, dataDir: '/fixture', serverVersion: 'test' } as const;

describe('main editor spellcheck policy', () => {
  it('retains Electron default-on spellcheck in actual window options __F1047', () => {
    createMainWindow({ server: SERVER, shellVersion: 'test', interfaceScale: 1 });
    expect(fake.options.at(-1)?.webPreferences?.spellcheck).not.toBe(false);
  });

  it('puts a right-click menu behind the squiggles: the app window listens for context-menu', () => {
    const window = createMainWindow({ server: SERVER, shellVersion: 'test', interfaceScale: 1 }) as unknown as InstanceType<typeof fake.BrowserWindow>;
    expect(window.contentsEvents).not.toContain('context-menu');
    installContextMenu({ window: window as unknown as BrowserWindow });
    expect(window.contentsEvents).toContain('context-menu');
  });
});

describe('window recreation geometry (MS-01)', () => {
  type Fake = InstanceType<typeof fake.BrowserWindow>;

  it('opens at the default size when there is nothing to carry (MS-01)', () => {
    createMainWindow({ server: SERVER, shellVersion: 'test', interfaceScale: 1 });
    const options = fake.options.at(-1)!;
    expect([options.width, options.height]).toEqual([1440, 900]);
    expect(options.x).toBeUndefined();
  });

  it("takes the previous window's bounds, kept above the minimum size (MS-01)", () => {
    createMainWindow({ server: SERVER, shellVersion: 'test', interfaceScale: 1, bounds: { x: 40, y: 60, width: 1200, height: 800 } });
    expect(fake.options.at(-1)).toMatchObject({ x: 40, y: 60, width: 1200, height: 800 });
    createMainWindow({ server: SERVER, shellVersion: 'test', interfaceScale: 1, bounds: { x: 0, y: 0, width: 300, height: 200 } });
    expect(fake.options.at(-1)).toMatchObject({ width: 900, height: 600 });
  });

  it('maximizes before showing when the previous window was maximized (MS-01)', () => {
    const plain = createMainWindow({ server: SERVER, shellVersion: 'test', interfaceScale: 1 }) as unknown as Fake;
    plain.onceHandlers.get('ready-to-show')?.();
    expect(plain.calls).toEqual(['show']);
    const maximized = createMainWindow({ server: SERVER, shellVersion: 'test', interfaceScale: 1, maximized: true }) as unknown as Fake;
    maximized.onceHandlers.get('ready-to-show')?.();
    expect(maximized.calls).toEqual(['maximize', 'show']);
  });
});

it('loads the remote New channel page for a local-server handoff', () => {
  createMainWindow({ server: SERVER, shellVersion: 'test', interfaceScale: 1, newChannel: true });
  expect(fake.loaded.at(-1)).toBe(`${SERVER.url}/#new-channel`);
});
