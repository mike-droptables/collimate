import type { MenuItemConstructorOptions } from 'electron';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryLog } from '../src/main/log.js';
import {
  installGuestViews,
  isGuestUrl,
  isManagedPartition,
  isWebUrl,
  type GuestIpcEvent,
  type GuestIpcLike,
  type GuestSessionLike,
  type GuestViewLike,
  type GuestViewsDeps,
} from '../src/main/guestViews.js';
import * as bridge from '../src/shared/bridge.js';
import { IPC, type GuestEvent } from '../src/shared/bridge.js';

/**
 * The guest manager against fakes, in the harness's pure-injection style:
 * one fake view factory, one fake session, one fake ipcMain, and a window
 * whose webContents is just an object with identity. Nothing here touches
 * Electron — `installGuestViews` takes every effect through deps, and this
 * file is the proof that the whole authorisation chokepoint, the
 * anti-flash visibility deferral, the layout math, and the capture retry
 * ladder are decidable without a compositor.
 */

// ---------------------------------------------------------------------------
// fakes
// ---------------------------------------------------------------------------

class FakeContents {
  loadedUrls: string[] = [];
  loadFailure: unknown = undefined;
  evaluations: { js: string; userGesture: boolean | undefined }[] = [];
  reloads = 0;
  devToolsOpens = 0;
  closes: { waitForBeforeUnload: boolean }[] = [];
  muted: boolean[] = [];
  destroyed = false;
  url = 'about:blank';
  canGoBackValue = false;
  canGoForwardValue = false;
  backs = 0;
  forwards = 0;
  windowOpenHandler: ((details: { url: string }) => { action: 'deny' }) | null = null;
  /** What executeJavaScript resolves with. */
  evaluateResult: unknown = undefined;
  /** Behaviour ladder for capturePage, consumed left to right. */
  captureScript: ('ok' | 'empty' | 'viz-error' | 'other-error')[] = ['ok'];
  encodings: string[] = [];
  captures: { stayHidden: boolean }[] = [];
  replacements: string[] = [];
  dictionaryWords: string[] = [];
  /** Every device-facing call, in order: `ua:<string>`, `metrics:<json>`, `clear-metrics`, `no-metrics` (the CDP session detached), `reload`. */
  deviceCalls: string[] = [];
  /** What a CDP command waits on before it answers (immediately by default). */
  metricsSettled: Promise<void> = Promise.resolve();
  attached = false;
  readonly debugger = {
    isAttached: (): boolean => this.attached,
    attach: (): void => {
      this.attached = true;
    },
    detach: (): void => {
      this.attached = false;
      this.deviceCalls.push('no-metrics');
    },
    sendCommand: async (method: string, params?: unknown): Promise<unknown> => {
      this.deviceCalls.push(method === 'Emulation.clearDeviceMetricsOverride'
        ? 'clear-metrics' : `metrics:${JSON.stringify(params)}`);
      await this.metricsSettled;
      return {};
    },
  };

  readonly session = {
    addWordToSpellCheckerDictionary: (word: string): boolean => {
      this.dictionaryWords.push(word);
      return true;
    },
  };

  private readonly listeners = new Map<string, Array<(...args: unknown[]) => void>>();

  navigationHistory = {
    canGoBack: () => this.canGoBackValue,
    canGoForward: () => this.canGoForwardValue,
    goBack: () => {
      this.backs++;
    },
    goForward: () => {
      this.forwards++;
    },
  };

  async loadURL(url: string): Promise<void> {
    this.loadedUrls.push(url);
    if (this.loadFailure !== undefined) throw this.loadFailure;
  }

  async executeJavaScript(js: string, userGesture?: boolean): Promise<unknown> {
    this.evaluations.push({ js, userGesture });
    return this.evaluateResult;
  }

  async capturePage(
    _rect?: undefined,
    options?: { stayHidden?: boolean },
  ): Promise<{ toPNG(): Buffer; toJPEG(quality: number): Buffer; isEmpty(): boolean }> {
    this.captures.push({ stayHidden: options?.stayHidden === true });
    const behaviour = this.captureScript.shift() ?? 'ok';
    if (behaviour === 'viz-error') throw new Error('UnknownVizError: surface has no frames');
    if (behaviour === 'other-error') throw new Error('something else entirely');
    const empty = behaviour === 'empty';
    return {
      toPNG: () => { this.encodings.push('png'); return Buffer.from([1, 2, 3]); },
      toJPEG: (quality: number) => { this.encodings.push(`jpeg${quality}`); return Buffer.from([255, 216, 255]); },
      isEmpty: () => empty,
    };
  }

  getURL(): string {
    return this.url;
  }

  reload(): void {
    this.reloads++;
    this.deviceCalls.push('reload');
  }

  setUserAgent(userAgent: string): void {
    this.deviceCalls.push(`ua:${userAgent}`);
  }

  openDevTools(): void {
    this.devToolsOpens++;
  }

  close(options?: { waitForBeforeUnload: boolean }): void {
    this.closes.push({ waitForBeforeUnload: options?.waitForBeforeUnload === true });
    this.destroyed = true;
  }

  isDestroyed(): boolean {
    return this.destroyed;
  }

  setWindowOpenHandler(handler: (details: { url: string }) => { action: 'deny' }): void {
    this.windowOpenHandler = handler;
  }

  setAudioMuted(muted: boolean): void {
    this.muted.push(muted);
  }

  replaceMisspelling(text: string): void {
    this.replacements.push(text);
  }

  on(event: string, listener: (...args: unknown[]) => void): this {
    const existing = this.listeners.get(event);
    if (existing) existing.push(listener);
    else this.listeners.set(event, [listener]);
    return this;
  }

  once(event: string, listener: (...args: unknown[]) => void): this {
    return this.on(event, listener);
  }

  /** Deliver an event exactly as the real webContents would. */
  emit(event: string, ...args: unknown[]): void {
    for (const listener of [...(this.listeners.get(event) ?? [])]) listener(...args);
  }
}

class FakeView {
  readonly contents = new FakeContents();
  bounds: { x: number; y: number; width: number; height: number }[] = [];
  visibility: boolean[] = [];
  radii: number[] = [];

  constructor(readonly options: { partition: string; userAgent?: string }) {}

  get webContents(): FakeContents {
    return this.contents;
  }

  setBounds(bounds: { x: number; y: number; width: number; height: number }): void {
    this.bounds.push(bounds);
  }

  setVisible(visible: boolean): void {
    this.visibility.push(visible);
  }

  setBorderRadius(radius: number): void {
    this.radii.push(radius);
  }

  get visible(): boolean {
    return this.visibility.at(-1) === true;
  }

  asView(): GuestViewLike {
    return this as unknown as GuestViewLike;
  }
}

class FakeSession implements GuestSessionLike {
  requestHandler: ((contents: unknown, permission: string, callback: (granted: boolean) => void) => void) | null =
    null;
  checkHandler: ((contents: unknown, permission: string) => boolean) | null = null;
  requestHandlerInstalls = 0;
  cleared = 0;
  downloadListeners: unknown[] = [];
  on(event: string, listener: unknown): this {
    if (event === 'will-download') this.downloadListeners.push(listener);
    return this;
  }

  setPermissionRequestHandler(
    handler: (contents: unknown, permission: string, callback: (granted: boolean) => void) => void,
  ): void {
    this.requestHandler = handler;
    this.requestHandlerInstalls++;
  }

  setPermissionCheckHandler(handler: (contents: unknown, permission: string) => boolean): void {
    this.checkHandler = handler;
  }

  async clearStorageData(): Promise<void> {
    this.cleared++;
  }

  /** What the request handler answers for `permission`. */
  requests(permission: string): boolean {
    let granted: boolean | null = null;
    this.requestHandler?.({}, permission, (value) => {
      granted = value;
    });
    return granted === true;
  }
}

class FakeIpc implements GuestIpcLike {
  readonly handlers = new Map<string, (event: GuestIpcEvent, payload: unknown) => Promise<unknown> | unknown>();
  readonly listeners = new Map<string, Array<(event: GuestIpcEvent, payload: unknown) => void>>();

  handle(channel: string, handler: (event: GuestIpcEvent, payload: unknown) => Promise<unknown> | unknown): void {
    if (this.handlers.has(channel)) throw new Error(`second handler for ${channel}`);
    this.handlers.set(channel, handler);
  }

  removeHandler(channel: string): void {
    this.handlers.delete(channel);
  }

  on(channel: string, listener: (event: GuestIpcEvent, payload: unknown) => void): this {
    const existing = this.listeners.get(channel);
    if (existing) existing.push(listener);
    else this.listeners.set(channel, [listener]);
    return this;
  }

  removeListener(channel: string, listener: (event: GuestIpcEvent, payload: unknown) => void): this {
    const existing = this.listeners.get(channel) ?? [];
    this.listeners.set(
      channel,
      existing.filter((entry) => entry !== listener),
    );
    return this;
  }

  invoke(channel: string, event: GuestIpcEvent, payload: unknown): Promise<unknown> {
    const handler = this.handlers.get(channel);
    if (!handler) return Promise.reject(new Error(`no handler for ${channel}`));
    return Promise.resolve().then(() => handler(event, payload));
  }

  send(channel: string, event: GuestIpcEvent, payload: unknown): void {
    for (const listener of [...(this.listeners.get(channel) ?? [])]) listener(event, payload);
  }
}

const PARTITION = 'persist:collimate-test';
const SHELL_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Collimate/0.0.0 Chrome/142.0.7444.59 Electron/43.2.0 Safari/537.36';
const PHONE_UA = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Mobile Safari/537.36';
const TABLET_UA = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36';
const metrics = (screenWidth: number, screenHeight: number): string => `metrics:${JSON.stringify({
  width: 0,
  height: 0,
  deviceScaleFactor: 0,
  mobile: true,
  screenWidth,
  screenHeight,
})}`;
const URL_OK = 'https://example.test/';

class Rig {
  readonly log = new MemoryLog();
  readonly ipc = new FakeIpc();
  readonly views: FakeView[] = [];
  readonly sessions = new Map<string, FakeSession>();
  readonly pushed: GuestEvent[] = [];
  sleeps = 0;
  hostDestroyed = false;
  addedViews: GuestViewLike[] = [];
  removedViews: GuestViewLike[] = [];
  /** Every right-click menu a guest popped, in order. */
  readonly contextMenus: MenuItemConstructorOptions[][] = [];
  closedListener: (() => void) | null = null;
  readonly dispose: () => void;

  private readonly hostNavigateListeners: Array<() => void> = [];

  readonly hostContents = {
    send: (_channel: string, payload: unknown) => {
      this.pushed.push(payload as GuestEvent);
    },
    isDestroyed: () => this.hostDestroyed,
    on: (_event: 'did-navigate', listener: () => void) => {
      this.hostNavigateListeners.push(listener);
    },
    off: (_event: 'did-navigate', listener: () => void) => {
      const index = this.hostNavigateListeners.indexOf(listener);
      if (index >= 0) this.hostNavigateListeners.splice(index, 1);
    },
  };

  /** The app window itself navigated (a reload). */
  navigateHost(): void {
    for (const listener of [...this.hostNavigateListeners]) listener();
  }

  constructor(isDev = true) {
    const deps: GuestViewsDeps = {
      createView: (options) => {
        const view = new FakeView(options);
        this.views.push(view);
        return view.asView();
      },
      shellUserAgent: () => SHELL_UA,
      chromeVersion: '142.0.7444.59',
      sessionFor: (partition) => this.sessionFor(partition),
      ipc: this.ipc,
      sleep: async () => {
        this.sleeps++;
      },
      popupContextMenu: (template) => {
        this.contextMenus.push(template);
      },
    };
    this.dispose = installGuestViews({
      window: {
        webContents: this.hostContents,
        contentView: {
          addChildView: (view) => {
            this.addedViews.push(view);
          },
          removeChildView: (view) => {
            this.removedViews.push(view);
          },
        },
        once: (_event, listener) => {
          this.closedListener = listener;
        },
      },
      log: this.log,
      isDev,
      deps,
    });
  }

  sessionFor(partition: string): FakeSession {
    let session = this.sessions.get(partition);
    if (!session) {
      session = new FakeSession();
      this.sessions.set(partition, session);
    }
    return session;
  }

  invoke(channel: string, payload: unknown, sender: unknown = this.hostContents): Promise<unknown> {
    return this.ipc.invoke(channel, { sender }, payload);
  }

  async create(guestId = 'g1', overrides: Record<string, unknown> = {}): Promise<FakeView> {
    await this.invoke(IPC.guestCreate, { guestId, partition: PARTITION, url: URL_OK, ...overrides });
    const view = this.views.at(-1);
    if (!view) throw new Error('createGuest built no view');
    return view;
  }

  layout(
    rects: Array<{ guestId: string; x: number; y: number; w: number; h: number; radius?: number }>,
    zoomFactor = 1,
    sender: unknown = this.hostContents,
  ): void {
    this.ipc.send(IPC.guestLayout, { sender }, { guests: rects, zoomFactor });
  }
}

async function refusalOf(promise: Promise<unknown>): Promise<string | null> {
  try {
    await promise;
    return null;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return /collimate-guest\/([a-z-]+)/.exec(message)?.[1] ?? null;
  }
}

let rig: Rig;
beforeEach(() => {
  rig = new Rig();
});

// ---------------------------------------------------------------------------
// creation
// ---------------------------------------------------------------------------

describe('createGuest', () => {
  it('builds a view for the partition, attaches it invisible, and starts the load', async () => {
    const state = await rig.invoke(IPC.guestCreate, {
      guestId: 'g1',
      partition: PARTITION,
      url: URL_OK,
    });
    expect(state).toEqual({ guestId: 'g1', url: URL_OK });
    const view = rig.views[0]!;
    expect(view.options.partition).toBe(PARTITION);
    expect(rig.addedViews).toContain(view.asView());
    // Invisible until asked AND laid out — but with real, on-screen
    // initial bounds so macOS starts producing frames.
    expect(view.visibility).toEqual([false]);
    expect(view.bounds).toHaveLength(1);
    expect(view.bounds[0]!.width).toBeGreaterThan(0);
    expect(view.webContents.loadedUrls).toEqual([URL_OK]);
  });

  it('a desktop guest keeps the shell\'s own user agent; a phone or tablet one gets its composed one (GN-04)', async () => {
    expect((await rig.create('g-default')).options.userAgent).toBeUndefined();
    expect((await rig.create('g-desktop', { device: 'desktop' })).options.userAgent).toBeUndefined();
    expect((await rig.create('g-phone', { device: 'phone' })).options.userAgent).toBe(PHONE_UA);
    expect((await rig.create('g-tablet', { device: 'tablet' })).options.userAgent).toBe(TABLET_UA);
  });

  it('a device guest re-applies its screen metrics on every main-frame navigation (GN-04)', async () => {
    const phone = await rig.create('g-phone', { device: 'phone' });
    const desktop = await rig.create('g-desktop');
    phone.webContents.emit('did-navigate', {}, 'https://m.example.test/');
    desktop.webContents.emit('did-navigate', {}, 'https://example.test/');
    expect(phone.webContents.deviceCalls).toEqual([metrics(412, 915)]);
    expect(desktop.webContents.deviceCalls).toEqual([]);
  });

  it('refuses a device it does not know (GN-04)', async () => {
    for (const device of ['watch', '', 3, null]) {
      expect(await refusalOf(rig.create('g-bad', { device }))).toBe('bad-argument');
    }
    expect(rig.views).toHaveLength(0);
  });

  it('refuses a second guest by the same name', async () => {
    await rig.create('g1');
    expect(await refusalOf(rig.create('g1'))).toBe('duplicate-guest');
  });

  it('refuses a partition outside the managed prefix', async () => {
    for (const partition of ['persist:evil', 'collimate-x', 'persist:collimate-', '']) {
      expect(await refusalOf(rig.create('g-bad', { partition }))).toBe('bad-partition');
    }
    expect(rig.views).toHaveLength(0);
  });

  it('refuses anything but http(s) URLs', async () => {
    for (const url of ['file:///etc/passwd', 'javascript:alert(1)', 'not a url', '']) {
      expect(await refusalOf(rig.create('g-bad', { url }))).toBe('bad-url');
    }
    expect(rig.views).toHaveLength(0);
  });

  it('accepts the blank page — a session that starts with no site', async () => {
    const view = await rig.create('g-blank', { url: 'about:blank' });
    expect(view.webContents.loadedUrls).toEqual(['about:blank']);
  });

  it('refuses a malformed guest id', async () => {
    for (const guestId of ['', 42, null, undefined, 'x'.repeat(300)]) {
      expect(await refusalOf(rig.create('ignored', { guestId }))).toBe('bad-argument');
    }
  });

  it('answers only the app window', async () => {
    expect(
      await refusalOf(rig.invoke(IPC.guestCreate, { guestId: 'g1', partition: PARTITION, url: URL_OK }, {})),
    ).toBe('not-embedder');
  });

  it('hardens the partition session deny-by-default, once', async () => {
    await rig.create('g1');
    await rig.create('g2');
    const session = rig.sessionFor(PARTITION);
    expect(session.requestHandlerInstalls).toBe(1);
    expect(session.requests('fullscreen')).toBe(true);
    expect(session.requests('clipboard-sanitized-write')).toBe(true);
    for (const denied of ['media', 'geolocation', 'notifications', 'midi', 'anything-new']) {
      expect(session.requests(denied)).toBe(false);
      expect(session.checkHandler?.({}, denied)).toBe(false);
    }
  });

  it('denies popups, full stop', async () => {
    const view = await rig.create('g1');
    expect(view.webContents.windowOpenHandler?.({ url: 'https://popup.test/' })).toEqual({ action: 'deny' });
  });
});

// ---------------------------------------------------------------------------
// addressing
// ---------------------------------------------------------------------------

describe('the chokepoint', () => {
  it('refuses an id it never created', async () => {
    expect(await refusalOf(rig.invoke(IPC.guestEvaluate, { guestId: 'nope', js: '1' }))).toBe('unknown-guest');
  });

  it('refuses a non-string id', async () => {
    expect(await refusalOf(rig.invoke(IPC.guestEvaluate, { guestId: 7, js: '1' }))).toBe('bad-argument');
  });

  it('refuses an overlong id on lookup, not just on create', async () => {
    expect(await refusalOf(rig.invoke(IPC.guestEvaluate, { guestId: 'x'.repeat(300), js: '1' }))).toBe(
      'bad-argument',
    );
  });

  it('refuses any op from a sender that is not the app window', async () => {
    await rig.create('g1');
    for (const channel of [
      IPC.guestEvaluate,
      IPC.guestCapturePng,
      IPC.guestCapturePlaceholder,
      IPC.guestCurrentUrl,
      IPC.guestNavigate,
      IPC.guestGoBack,
      IPC.guestGoForward,
      IPC.guestReload,
      IPC.guestOpenDevTools,
      IPC.guestDestroy,
      IPC.guestSetVisible,
      IPC.guestRaise,
    ]) {
      expect(await refusalOf(rig.invoke(channel, { guestId: 'g1' }, {}))).toBe('not-embedder');
    }
  });

  it('reports a guest whose renderer is gone, and forgets it', async () => {
    const view = await rig.create('g1');
    view.webContents.destroyed = true;
    expect(await refusalOf(rig.invoke(IPC.guestEvaluate, { guestId: 'g1', js: '1' }))).toBe('guest-gone');
    // Forgotten: the second call is unknown, not gone.
    expect(await refusalOf(rig.invoke(IPC.guestEvaluate, { guestId: 'g1', js: '1' }))).toBe('unknown-guest');
  });
});

describe('guest operations', () => {
  it('evaluate runs with a user gesture and returns the completion value', async () => {
    const view = await rig.create('g1');
    view.webContents.evaluateResult = 2;
    const result = await rig.invoke(IPC.guestEvaluate, { guestId: 'g1', js: '1 + 1' });
    expect(result).toBe(2);
    expect(view.webContents.evaluations).toEqual([{ js: '1 + 1', userGesture: true }]);
  });

  it('navigate gates to http(s), plus the blank page', async () => {
    const view = await rig.create('g1');
    await rig.invoke(IPC.guestNavigate, { guestId: 'g1', url: 'https://elsewhere.test/' });
    await rig.invoke(IPC.guestNavigate, { guestId: 'g1', url: 'about:blank' });
    expect(view.webContents.loadedUrls).toEqual([URL_OK, 'https://elsewhere.test/', 'about:blank']);
    expect(await refusalOf(rig.invoke(IPC.guestNavigate, { guestId: 'g1', url: 'file:///x' }))).toBe('bad-url');
  });

  it('F0994 installs silent download interception on the actual managed session', async () => {
    await rig.create('g1');
    expect(rig.sessions.get(PARTITION)?.downloadListeners).toHaveLength(1);
    await rig.create('g2');
    expect(rig.sessions.get(PARTITION)?.downloadListeners).toHaveLength(1);
  });

  it('F0996 accepts structured superseded navigation while retaining real failures', async () => {
    const view = await rig.create('g1');
    view.contents.loadFailure = Object.assign(new Error('superseded'), { code: 'ERR_ABORTED', errno: -3 });
    await expect(rig.invoke(IPC.guestNavigate, { guestId: 'g1', url: URL_OK })).resolves.toBeUndefined();
    view.contents.loadFailure = Object.assign(new Error('offline ERR_ABORTED text'), { code: 'ERR_INTERNET_DISCONNECTED', errno: -106 });
    await expect(rig.invoke(IPC.guestNavigate, { guestId: 'g1', url: URL_OK })).rejects.toThrow('offline');
    view.contents.loadFailure = new Error('ERR_ABORTED');
    await expect(rig.invoke(IPC.guestNavigate, { guestId: 'g1', url: URL_OK })).rejects.toThrow('ERR_ABORTED');
  });

  it('F0996 keeps navigation pending until the actual load settles', async () => {
    const view = await rig.create('g1');
    let resolve!: () => void;
    view.contents.loadURL = () => new Promise<void>((done) => { resolve = done; });
    let settled = false;
    const pending = rig.invoke(IPC.guestNavigate, { guestId: 'g1', url: URL_OK }).then(() => { settled = true; });
    await Promise.resolve();
    await Promise.resolve();
    expect(settled).toBe(false);
    resolve();
    await pending;
    expect(settled).toBe(true);
  });

  it('goBack and goForward answer false when there is nowhere to go', async () => {
    const view = await rig.create('g1');
    expect(await rig.invoke(IPC.guestGoBack, { guestId: 'g1' })).toBe(false);
    expect(await rig.invoke(IPC.guestGoForward, { guestId: 'g1' })).toBe(false);
    view.webContents.canGoBackValue = true;
    view.webContents.canGoForwardValue = true;
    expect(await rig.invoke(IPC.guestGoBack, { guestId: 'g1' })).toBe(true);
    expect(await rig.invoke(IPC.guestGoForward, { guestId: 'g1' })).toBe(true);
    expect(view.webContents.backs).toBe(1);
    expect(view.webContents.forwards).toBe(1);
  });

  it('devtools are a dev-build affordance', async () => {
    const packaged = new Rig(false);
    await packaged.create('g1');
    expect(await refusalOf(packaged.invoke(IPC.guestOpenDevTools, { guestId: 'g1' }))).toBe(
      'devtools-unavailable',
    );

    await rig.create('g1');
    await rig.invoke(IPC.guestOpenDevTools, { guestId: 'g1' });
    expect(rig.views[0]!.webContents.devToolsOpens).toBe(1);
  });

  it('setGuestDevice sets the user agent, then the screen metrics, then reloads in place (GN-04)', async () => {
    const view = await rig.create('g1');
    await rig.invoke(IPC.guestSetDevice, { guestId: 'g1', device: 'phone' });
    expect(view.webContents.deviceCalls).toEqual([`ua:${PHONE_UA}`, metrics(412, 915), 'reload']);
    view.webContents.deviceCalls.length = 0;
    await rig.invoke(IPC.guestSetDevice, { guestId: 'g1', device: 'tablet' });
    expect(view.webContents.deviceCalls).toEqual([`ua:${TABLET_UA}`, metrics(800, 1280), 'reload']);
    view.webContents.deviceCalls.length = 0;
    await rig.invoke(IPC.guestSetDevice, { guestId: 'g1', device: 'desktop' });
    expect(view.webContents.deviceCalls).toEqual([`ua:${SHELL_UA}`, 'clear-metrics', 'no-metrics', 'reload']);
    // The same guest throughout: no view built, none torn down.
    expect(rig.views).toHaveLength(1);
    expect(view.webContents.closes).toEqual([]);
    // Back on desktop, a navigation applies no metrics.
    view.webContents.deviceCalls.length = 0;
    view.webContents.emit('did-navigate', {}, URL_OK);
    expect(view.webContents.deviceCalls).toEqual([]);
  });

  it('setGuestDevice reloads only once the browser has the new screen (GN-04)', async () => {
    const view = await rig.create('g1');
    let settle!: () => void;
    view.webContents.metricsSettled = new Promise<void>((resolve) => { settle = resolve; });
    const switching = rig.invoke(IPC.guestSetDevice, { guestId: 'g1', device: 'phone' });
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(view.webContents.deviceCalls).toEqual([`ua:${PHONE_UA}`, metrics(412, 915)]);
    settle();
    await switching;
    expect(view.webContents.deviceCalls).toEqual([`ua:${PHONE_UA}`, metrics(412, 915), 'reload']);
  });

  it('setGuestDevice refuses an unknown device and an unknown guest (GN-04)', async () => {
    const view = await rig.create('g1');
    for (const device of ['watch', undefined, 1]) {
      expect(await refusalOf(rig.invoke(IPC.guestSetDevice, { guestId: 'g1', device }))).toBe('bad-argument');
    }
    expect(view.webContents.deviceCalls).toEqual([]);
    expect(await refusalOf(rig.invoke(IPC.guestSetDevice, { guestId: 'nope', device: 'phone' }))).toBe('unknown-guest');
    expect(await refusalOf(rig.invoke(IPC.guestSetDevice, { guestId: 'g1', device: 'phone' }, {}))).toBe('not-embedder');
  });

  it('clearPartition wipes only managed partitions', async () => {
    await rig.invoke(IPC.clearPartition, { partition: PARTITION });
    expect(rig.sessionFor(PARTITION).cleared).toBe(1);
    expect(await refusalOf(rig.invoke(IPC.clearPartition, { partition: 'persist:other' }))).toBe('bad-partition');
  });
});

// ---------------------------------------------------------------------------
// visibility + anti-flash
// ---------------------------------------------------------------------------

describe('visibility', () => {
  it('defers a show until the first layout, then applies it', async () => {
    const view = await rig.create('g1');
    await rig.invoke(IPC.guestSetVisible, { guestId: 'g1', visible: true });
    // Still hidden: nothing has positioned it yet.
    expect(view.visible).toBe(false);
    rig.layout([{ guestId: 'g1', x: 10, y: 20, w: 300, h: 200 }]);
    expect(view.visible).toBe(true);
  });

  it('shows immediately once laid out', async () => {
    const view = await rig.create('g1');
    rig.layout([{ guestId: 'g1', x: 0, y: 0, w: 100, h: 100 }]);
    expect(view.visible).toBe(false);
    await rig.invoke(IPC.guestSetVisible, { guestId: 'g1', visible: true });
    expect(view.visible).toBe(true);
  });

  it('a hide before the first layout cancels a deferred show', async () => {
    const view = await rig.create('g1');
    await rig.invoke(IPC.guestSetVisible, { guestId: 'g1', visible: true });
    await rig.invoke(IPC.guestSetVisible, { guestId: 'g1', visible: false });
    rig.layout([{ guestId: 'g1', x: 0, y: 0, w: 100, h: 100 }]);
    expect(view.visible).toBe(false);
  });

  it('mutes on hide and unmutes on show, but only when asked to', async () => {
    const muting = await rig.create('muting', { muteWhenHidden: true });
    rig.layout([{ guestId: 'muting', x: 0, y: 0, w: 100, h: 100 }]);
    await rig.invoke(IPC.guestSetVisible, { guestId: 'muting', visible: false });
    expect(muting.webContents.muted).toEqual([true]);
    await rig.invoke(IPC.guestSetVisible, { guestId: 'muting', visible: true });
    expect(muting.webContents.muted).toEqual([true, false]);

    const loud = await rig.create('loud');
    rig.layout([{ guestId: 'loud', x: 0, y: 0, w: 100, h: 100 }]);
    await rig.invoke(IPC.guestSetVisible, { guestId: 'loud', visible: false });
    expect(loud.webContents.muted).toEqual([]);
  });

  it('refuses a non-boolean visible', async () => {
    await rig.create('g1');
    expect(await refusalOf(rig.invoke(IPC.guestSetVisible, { guestId: 'g1', visible: 'yes' }))).toBe(
      'bad-argument',
    );
  });
});

// ---------------------------------------------------------------------------
// layout
// ---------------------------------------------------------------------------

describe('the layout channel', () => {
  it('F1025 exposes no production refusal parser for test fixtures', () => {
    expect(Object.hasOwn(bridge, 'guestRefusalOf')).toBe(false);
  });

  it('F1026 keeps hidden motion geometry current without resending unchanged native radius', async () => {
    const view = await rig.create('moving');
    const initialBounds = view.bounds.length;
    for (let frame = 0; frame < 60; frame++) {
      rig.layout([{ guestId: 'moving', x: frame, y: frame * 2, w: 300 + frame, h: 200, radius: 4.1 }]);
    }
    console.info('F1026 hidden motion measurement', JSON.stringify({
      frames: 60, boundsUpdates: view.bounds.length - initialBounds,
      radiusUpdates: view.radii.length, visible: view.visible,
    }));
    expect(view.visible).toBe(false);
    expect(view.visibility).toEqual([false]);
    expect(view.bounds.length - initialBounds).toBe(60);
    expect(view.bounds.at(-1)).toEqual({ x: 59, y: 118, width: 359, height: 200 });
    expect(view.radii).toEqual([4]);

    // Subpixel radius changes that round to the same native value need no update.
    rig.layout([{ guestId: 'moving', x: 60, y: 120, w: 360, h: 200, radius: 4.2 }]);
    rig.layout([{ guestId: 'moving', x: 60, y: 120, w: 360, h: 200, radius: -1 }]);
    rig.layout([{ guestId: 'moving', x: 60, y: 120, w: 360, h: 200, radius: Number.NaN }]);
    expect(view.radii).toEqual([4]);
    rig.layout([{ guestId: 'moving', x: 60, y: 120, w: 360, h: 200, radius: 4.2 }], 2);
    rig.layout([{ guestId: 'moving', x: 60, y: 120, w: 360, h: 200, radius: 0 }], 2);
    expect(view.radii).toEqual([4, 8, 0]);
    await rig.invoke(IPC.guestSetVisible, { guestId: 'moving', visible: true });
    expect(view.visible).toBe(true);
    expect(view.bounds.at(-1)).toEqual({ x: 120, y: 240, width: 720, height: 400 });

    await rig.invoke(IPC.guestDestroy, { guestId: 'moving' });
    const replacement = await rig.create('moving');
    rig.layout([{ guestId: 'moving', x: 0, y: 0, w: 100, h: 100, radius: 0 }]);
    expect(replacement.radii).toEqual([0]);
  });

  it('applies zoom-scaled, rounded bounds and radius', async () => {
    const view = await rig.create('g1');
    rig.layout([{ guestId: 'g1', x: 10.4, y: 20.5, w: 300.2, h: 199.6, radius: 4.4 }], 2);
    expect(view.bounds.at(-1)).toEqual({ x: 21, y: 41, width: 600, height: 399 });
    expect(view.radii.at(-1)).toBe(9);
  });

  it('is an identity on renderer-snapped integers at zoom 1', async () => {
    const view = await rig.create('g1');
    rig.layout([{ guestId: 'g1', x: 12, y: 34, w: 560, h: 320, radius: 4 }]);
    expect(view.bounds.at(-1)).toEqual({ x: 12, y: 34, width: 560, height: 320 });
    expect(view.radii.at(-1)).toBe(4);
  });

  it('silently drops unknown ids, malformed rects, and foreign senders', async () => {
    const view = await rig.create('g1');
    const before = view.bounds.length;
    rig.layout([{ guestId: 'stranger', x: 0, y: 0, w: 1, h: 1 }]);
    rig.layout([{ guestId: 'g1', x: Number.NaN, y: 0, w: 1, h: 1 }]);
    rig.layout([{ guestId: 'g1', x: 0, y: 0, w: 1, h: 1 }], 1, {});
    expect(view.bounds.length).toBe(before);
  });

  it('never sends negative dimensions to the compositor', async () => {
    const view = await rig.create('g1');
    rig.layout([{ guestId: 'g1', x: 0, y: 0, w: -5, h: -5 }]);
    expect(view.bounds.at(-1)).toEqual({ x: 0, y: 0, width: 0, height: 0 });
  });
});

describe('raiseGuest', () => {
  it('re-adds the view, which is the native way to the top of the z-order', async () => {
    const view = await rig.create('g1');
    await rig.create('g2');
    await rig.invoke(IPC.guestRaise, { guestId: 'g1' });
    expect(rig.addedViews.at(-1)).toBe(view.asView());
    expect(rig.addedViews.filter((v) => v === view.asView())).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// events
// ---------------------------------------------------------------------------

describe('event forwarding', () => {
  it('pushes navigation, title, loading, ready and focus with the renderer-minted id', async () => {
    const view = await rig.create('g1');
    const contents = view.webContents;
    contents.emit('did-navigate', {}, 'https://a.test/');
    contents.emit('did-navigate-in-page', {}, 'https://a.test/#x', true);
    contents.emit('did-navigate-in-page', {}, 'https://ad.test/frame', false);
    contents.emit('page-title-updated', {}, 'Hello');
    contents.emit('did-start-loading');
    contents.emit('did-stop-loading');
    contents.url = 'https://a.test/';
    contents.emit('did-finish-load');
    contents.emit('focus');
    contents.emit('blur');
    expect(rig.pushed).toEqual([
      { guestId: 'g1', kind: 'navigate', data: { url: 'https://a.test/', inPage: false } },
      { guestId: 'g1', kind: 'navigate', data: { url: 'https://a.test/#x', inPage: true } },
      { guestId: 'g1', kind: 'title', data: { title: 'Hello' } },
      { guestId: 'g1', kind: 'loading', data: { loading: true } },
      { guestId: 'g1', kind: 'loading', data: { loading: false } },
      { guestId: 'g1', kind: 'ready', data: { url: 'https://a.test/' } },
      { guestId: 'g1', kind: 'focus', data: { focused: true } },
      { guestId: 'g1', kind: 'focus', data: { focused: false } },
    ]);
  });

  it('relays a wheel the guest took, in DOM terms, without stopping its native scroll', async () => {
    const view = await rig.create('g1');
    const contents = view.webContents;
    const wheelEvent = { preventDefault: vi.fn() };
    // Chromium's sign: negative scrolls down.
    contents.emit('input-event', wheelEvent, { type: 'mouseWheel', deltaX: 0, deltaY: -40, modifiers: [] });
    contents.emit('input-event', wheelEvent, { type: 'mouseWheel', deltaX: 12, deltaY: 3, modifiers: ['control'] });
    // Every other input the guest takes stays its own.
    contents.emit('input-event', { preventDefault: vi.fn() }, { type: 'mouseMove', modifiers: [] });
    contents.emit('input-event', { preventDefault: vi.fn() }, { type: 'keyDown', modifiers: [] });
    expect(rig.pushed).toEqual([
      { guestId: 'g1', kind: 'wheel', data: { deltaX: 0, deltaY: 40, ctrlKey: false } },
      { guestId: 'g1', kind: 'wheel', data: { deltaX: -12, deltaY: -3, ctrlKey: true } },
    ]);
    expect(wheelEvent.preventDefault).not.toHaveBeenCalled();
  });

  it('keeps a crashed guest addressable — the page decides what happens next', async () => {
    const view = await rig.create('g1');
    view.webContents.emit('render-process-gone', {}, { reason: 'crashed', exitCode: 139 });
    expect(rig.pushed).toContainEqual({
      guestId: 'g1',
      kind: 'crashed',
      data: { reason: 'crashed', exitCode: 139 },
    });
    // Still registered: a reload into the same view must route.
    await rig.invoke(IPC.guestReload, { guestId: 'g1' });
    expect(view.webContents.reloads).toBe(1);
  });

  it('answers a right-click with the native spelling and edit menu, acting on that guest', async () => {
    const view = await rig.create('g1');
    const contents = view.webContents;
    const editFlags = { canCut: false, canCopy: false, canPaste: true, canSelectAll: true };
    contents.emit('context-menu', {}, {
      misspelledWord: 'helo',
      dictionarySuggestions: ['hello', 'help'],
      isEditable: true,
      selectionText: '',
      editFlags,
    });
    const menu = rig.contextMenus.at(-1)!;
    expect(menu.map((item) => item.label ?? item.role ?? item.type)).toEqual([
      'hello', 'help', 'separator', 'Add to dictionary', 'separator', 'cut', 'copy', 'paste', 'separator', 'selectAll',
    ]);
    menu[1]!.click!({} as never, undefined, {} as never);
    menu[3]!.click!({} as never, undefined, {} as never);
    expect(contents.replacements).toEqual(['help']);
    expect(contents.dictionaryWords).toEqual(['helo']);
    // Nothing to offer over plain page content: no menu at all.
    contents.emit('context-menu', {}, {
      misspelledWord: '', dictionarySuggestions: [],
      isEditable: false, selectionText: '', editFlags,
    });
    expect(rig.contextMenus).toHaveLength(1);
    // The menu is the guest's own business: nothing reaches the page.
    expect(rig.pushed).toEqual([]);
  });

  it('forgets a guest whose webContents is destroyed underneath it', async () => {
    const view = await rig.create('g1');
    view.webContents.destroyed = true;
    view.webContents.emit('destroyed');
    expect(await refusalOf(rig.invoke(IPC.guestReload, { guestId: 'g1' }))).toBe('unknown-guest');
  });
});

// ---------------------------------------------------------------------------
// capture
// ---------------------------------------------------------------------------

describe('capturePng', () => {
  it('uses private JPEG85 placeholders and exact PNG screenshots (F1038)', async () => {
    const view = await rig.create('g1');
    const placeholder = await rig.invoke(IPC.guestCapturePlaceholder, { guestId: 'g1' });
    const screenshot = await rig.invoke(IPC.guestCapturePng, { guestId: 'g1' });
    expect([...new Uint8Array(placeholder as ArrayBuffer)]).toEqual([255, 216, 255]);
    expect([...new Uint8Array(screenshot as ArrayBuffer)]).toEqual([1, 2, 3]);
    expect(view.webContents.encodings).toEqual(['jpeg85', 'png']);
  });

  it('captures a visible guest without touching its visibility', async () => {
    const view = await rig.create('g1');
    rig.layout([{ guestId: 'g1', x: 0, y: 0, w: 100, h: 100 }]);
    await rig.invoke(IPC.guestSetVisible, { guestId: 'g1', visible: true });
    const flips = view.visibility.length;
    const bytes = await rig.invoke(IPC.guestCapturePng, { guestId: 'g1' });
    expect(bytes).toBeInstanceOf(ArrayBuffer);
    expect(view.visibility.length).toBe(flips);
    expect(view.webContents.captures).toEqual([{ stayHidden: false }]);
  });

  it('primes a hidden guest and captures it with stayHidden', async () => {
    const view = await rig.create('g1');
    await rig.invoke(IPC.guestCapturePng, { guestId: 'g1' });
    // The prime: on, then straight back off, before the capture.
    expect(view.visibility).toEqual([false, true, false]);
    expect(view.webContents.captures).toEqual([{ stayHidden: true }]);
  });

  it('retries transient viz errors a few frames apart', async () => {
    const view = await rig.create('g1');
    view.webContents.captureScript = ['viz-error', 'viz-error', 'ok'];
    const bytes = await rig.invoke(IPC.guestCapturePng, { guestId: 'g1' });
    expect(bytes).toBeInstanceOf(ArrayBuffer);
    expect(view.webContents.captures).toHaveLength(3);
    expect(rig.sleeps).toBe(2);
  });

  it('retries an empty frame, then hands over whatever the last attempt got', async () => {
    const view = await rig.create('g1');
    view.webContents.captureScript = ['empty', 'ok'];
    await rig.invoke(IPC.guestCapturePng, { guestId: 'g1' });
    expect(view.webContents.captures).toHaveLength(2);
  });

  it('gives up after five attempts and rethrows', async () => {
    const view = await rig.create('g1');
    view.webContents.captureScript = ['viz-error', 'viz-error', 'viz-error', 'viz-error', 'viz-error'];
    await expect(rig.invoke(IPC.guestCapturePng, { guestId: 'g1' })).rejects.toThrow(/viz/i);
    expect(view.webContents.captures).toHaveLength(5);
  });

  it('does not retry errors that are not the compositor being slow', async () => {
    const view = await rig.create('g1');
    view.webContents.captureScript = ['other-error'];
    await expect(rig.invoke(IPC.guestCapturePng, { guestId: 'g1' })).rejects.toThrow('something else');
    expect(view.webContents.captures).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// destroy + dispose
// ---------------------------------------------------------------------------

describe('destroyGuest', () => {
  it('removes the view and closes its renderer without waiting for beforeunload', async () => {
    const view = await rig.create('g1');
    await rig.invoke(IPC.guestDestroy, { guestId: 'g1' });
    expect(rig.removedViews).toContain(view.asView());
    expect(view.webContents.closes).toEqual([{ waitForBeforeUnload: false }]);
    expect(await refusalOf(rig.invoke(IPC.guestReload, { guestId: 'g1' }))).toBe('unknown-guest');
  });

  it('is idempotent — a second destroy is a resolved no-op', async () => {
    await rig.create('g1');
    await rig.invoke(IPC.guestDestroy, { guestId: 'g1' });
    await expect(rig.invoke(IPC.guestDestroy, { guestId: 'g1' })).resolves.toBeUndefined();
  });
});

describe('a reloading app window', () => {
  it('reaps every guest — the page that minted their ids is gone', async () => {
    const view = await rig.create('g1');
    rig.navigateHost();
    expect(view.webContents.closes).toEqual([{ waitForBeforeUnload: false }]);
    expect(rig.removedViews).toContain(view.asView());
    expect(await refusalOf(rig.invoke(IPC.guestReload, { guestId: 'g1' }))).toBe('unknown-guest');
    // …and the successor page can mint the same id afresh.
    await rig.create('g1');
  });

  it('is a no-op over an empty registry (the window\'s first load)', () => {
    expect(() => rig.navigateHost()).not.toThrow();
  });

  it('stops listening once disposed', async () => {
    const view = await rig.create('g1');
    rig.dispose();
    const closes = view.webContents.closes.length;
    rig.navigateHost();
    expect(view.webContents.closes.length).toBe(closes);
  });
});

describe('dispose', () => {
  it('uninstalls every channel and reclaims every guest', async () => {
    const view = await rig.create('g1');
    rig.dispose();
    expect(view.webContents.closes).toHaveLength(1);
    expect(rig.ipc.handlers.size).toBe(0);
    expect(rig.ipc.listeners.get(IPC.guestLayout)).toEqual([]);
    // A second dispose (the window's own closed hook firing later) is safe.
    rig.dispose();
  });

  it('is wired to the window closing', async () => {
    const view = await rig.create('g1');
    expect(rig.closedListener).not.toBeNull();
    rig.closedListener?.();
    expect(view.webContents.closes).toHaveLength(1);
    expect(rig.ipc.handlers.size).toBe(0);
  });

  it('stops pushing events after the host is gone', async () => {
    const view = await rig.create('g1');
    rig.dispose();
    const pushes = rig.pushed.length;
    view.webContents.emit('did-start-loading');
    expect(rig.pushed.length).toBe(pushes);
  });
});

// ---------------------------------------------------------------------------
// the predicates
// ---------------------------------------------------------------------------

describe('isManagedPartition', () => {
  it('accepts only non-empty names under the prefix', () => {
    expect(isManagedPartition('persist:collimate-x')).toBe(true);
    expect(isManagedPartition('persist:collimate-')).toBe(false);
    expect(isManagedPartition('persist:other-x')).toBe(false);
    expect(isManagedPartition('collimate-x')).toBe(false);
  });
});

describe('isWebUrl', () => {
  it('accepts http(s) and nothing else', () => {
    expect(isWebUrl('http://a.test/')).toBe(true);
    expect(isWebUrl('https://a.test/')).toBe(true);
    expect(isWebUrl('file:///etc/passwd')).toBe(false);
    expect(isWebUrl('javascript:alert(1)')).toBe(false);
    expect(isWebUrl('nonsense')).toBe(false);
    // The blank-page exception is the guest gate's, not this one's — the
    // OS-browser path (openExternally) is built on http(s) alone.
    expect(isWebUrl('about:blank')).toBe(false);
  });
});

describe('isGuestUrl', () => {
  it('is isWebUrl plus exactly the blank page', () => {
    expect(isGuestUrl('https://a.test/')).toBe(true);
    expect(isGuestUrl('about:blank')).toBe(true);
    expect(isGuestUrl('about:config')).toBe(false);
    expect(isGuestUrl('about:blank#x')).toBe(false);
    expect(isGuestUrl('file:///etc/passwd')).toBe(false);
    expect(isGuestUrl('javascript:alert(1)')).toBe(false);
    expect(isGuestUrl('')).toBe(false);
  });
});
