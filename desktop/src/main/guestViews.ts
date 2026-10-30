import {
  app,
  ipcMain,
  session,
  WebContentsView,
  type BaseWindow,
  type BrowserWindow,
  type InputEvent as GuestInputEvent,
  type MouseWheelInputEvent,
} from 'electron';
import {
  GUEST_REFUSAL_PREFIX,
  IPC,
  PARTITION_PREFIX,
  type GuestDevice,
  type GuestEvent,
  type GuestRefusal,
  type GuestState,
} from '../shared/bridge.js';
import {
  contextMenuListener,
  popupContextMenuIn,
  type ContextMenuParamsLike,
  type ContextMenuPopup,
  type ContextMenuTargetLike,
} from './contextMenu.js';
import type { LogSink } from './log.js';
import { GuestDownloads, type DownloadSessionLike } from './guestDownloads.js';
import { deviceMetrics, devicePresentation, isGuestDevice, type DeviceMetrics } from './guestDevices.js';

const guestDownloads = new GuestDownloads(() => app.getPath('downloads'));

/**
 * Guest views: creation, hardening, a registry, and the one place the
 * page is allowed to reach into them.
 *
 * Generators render arbitrary web pages, and the shell's answer is a
 * `WebContentsView` — a real, separately-sandboxed renderer owned by the
 * main process, not a DOM element the page constructs. Creation itself is
 * a bridge call now: the page asks, this module builds the view with
 * privileges it chose (never ones the page requested), and every later
 * operation is answered only for ids created through that call. There is
 * no attach event to guard and nothing to scrub — a guest the page didn't
 * create through here does not exist.
 *
 * The other thing that changed with the move off `<webview>`: geometry.
 * The native view paints above all renderer DOM, so the page keeps a
 * placeholder in its own layout and streams that placeholder's rect over
 * the fire-and-forget layout channel; this module only ever applies
 * bounds, it never computes them. Visibility is deferred until the first
 * layout arrives so a fresh view can't flash at its creation bounds.
 *
 * The guests themselves are built stripped: no node, no preload, sandbox
 * on, popups denied. A guest is treated as hostile content, because it is.
 */

/**
 * The only permissions a guest page gets without a human in the loop.
 *
 * Deny-by-default rather than a blocklist: camera, microphone,
 * geolocation, and notifications are the ones anyone thinks to ask about,
 * but the list Chromium can ask for grows every release, and a new one
 * should arrive denied rather than pre-granted. Fullscreen and a
 * page-initiated copy are the two that a rendered page plausibly needs and
 * that cost the user nothing.
 */
const GUEST_PERMISSIONS: ReadonlySet<string> = new Set(['fullscreen', 'clipboard-sanitized-write']);

/**
 * Sessions whose permission handlers are already installed.
 *
 * Handlers are per-`Session`, not per-guest, and setting one replaces the
 * last — so this is keyed on the session object itself and lives at module
 * scope, outside any one window's install, because that's the lifetime a
 * partition actually has.
 */
const hardenedSessions = new WeakSet<object>();

/**
 * Where a view sits before its first layout. On-screen on purpose:
 * macOS never starts rendering a view whose bounds are off-screen, so a
 * guest created at (-9999, -9999) would stay a white rectangle until it
 * was both laid out AND repainted. The view is invisible until the first
 * layout anyway, so these bounds are never seen.
 */
const INITIAL_BOUNDS = { x: 0, y: 0, width: 640, height: 480 };

/** Attempts + spacing for capture retries — see captureGuest. */
const CAPTURE_ATTEMPTS = 5;
const CAPTURE_RETRY_MS = 16;

/** A Chromium wheel delta (positive scrolls up/left) in DOM WheelEvent
 *  terms (positive scrolls down/right). */
function domWheelDelta(chromium: number | undefined): number {
  return chromium ? -chromium : 0;
}

/** A refusal the page can branch on; see GuestRefusal in shared/bridge.ts. */
class GuestRefusalError extends Error {
  constructor(
    readonly reason: GuestRefusal,
    detail: string,
  ) {
    super(`${GUEST_REFUSAL_PREFIX}${reason}: ${detail}`);
    this.name = 'GuestRefusalError';
  }
}

function refuse(reason: GuestRefusal, detail: string): GuestRefusalError {
  return new GuestRefusalError(reason, detail);
}

// ---------------------------------------------------------------------------
// Structural slices of Electron, so the whole manager runs against fakes.
// Same style as SessionLike in serverAccess.ts: the manager takes every
// effect through these, and the defaults built in installGuestViews are
// the only place the real Electron objects are touched.
// ---------------------------------------------------------------------------

/** The slice of a guest's `webContents` this module drives. The
 *  right-click menu's items act on it too (ContextMenuTargetLike). */
export interface GuestContentsLike extends ContextMenuTargetLike {
  loadURL(url: string): Promise<void>;
  executeJavaScript(js: string, userGesture?: boolean): Promise<unknown>;
  capturePage(
    rect?: undefined,
    options?: { stayHidden?: boolean },
  ): Promise<{ toPNG(): Buffer; toJPEG(quality: number): Buffer; isEmpty(): boolean }>;
  getURL(): string;
  navigationHistory: {
    canGoBack(): boolean;
    canGoForward(): boolean;
    goBack(): void;
    goForward(): void;
  };
  reload(): void;
  setUserAgent(userAgent: string): void;
  /** The guest's CDP session, which carries its device's screen metrics. */
  debugger: {
    isAttached(): boolean;
    attach(): void;
    detach(): void;
    sendCommand(method: 'Emulation.setDeviceMetricsOverride', params: DeviceMetrics): Promise<unknown>;
    sendCommand(method: 'Emulation.clearDeviceMetricsOverride'): Promise<unknown>;
  };
  openDevTools(options?: { mode: 'detach' }): void;
  close(options?: { waitForBeforeUnload: boolean }): void;
  isDestroyed(): boolean;
  setWindowOpenHandler(handler: (details: { url: string }) => { action: 'deny' }): void;
  setAudioMuted(muted: boolean): void;
  on(event: 'did-navigate', listener: (event: unknown, url: string) => void): unknown;
  on(
    event: 'did-navigate-in-page',
    listener: (event: unknown, url: string, isMainFrame: boolean) => void,
  ): unknown;
  on(event: 'page-title-updated', listener: (event: unknown, title: string) => void): unknown;
  on(event: 'did-start-loading', listener: () => void): unknown;
  on(event: 'did-stop-loading', listener: () => void): unknown;
  on(event: 'did-finish-load', listener: () => void): unknown;
  on(event: 'focus', listener: () => void): unknown;
  on(event: 'blur', listener: () => void): unknown;
  on(event: 'input-event', listener: (event: unknown, input: GuestInputEvent) => void): unknown;
  on(
    event: 'render-process-gone',
    listener: (event: unknown, details: { reason: string; exitCode: number }) => void,
  ): unknown;
  on(event: 'context-menu', listener: (event: unknown, params: ContextMenuParamsLike) => void): unknown;
  once(event: 'destroyed', listener: () => void): unknown;
}

/** The slice of a `WebContentsView` this module drives. */
export interface GuestViewLike {
  webContents: GuestContentsLike;
  setBounds(bounds: { x: number; y: number; width: number; height: number }): void;
  setVisible(visible: boolean): void;
  setBorderRadius(radius: number): void;
}

/** The slice of a guest partition's `Session` the hardening needs. */
export interface GuestSessionLike extends DownloadSessionLike {
  setPermissionRequestHandler(
    handler: (contents: unknown, permission: string, callback: (granted: boolean) => void) => void,
  ): void;
  setPermissionCheckHandler(handler: (contents: unknown, permission: string) => boolean): void;
  clearStorageData(): Promise<void>;
}

/** The slice of the app window guests are children of. */
export interface GuestWindowLike {
  webContents: {
    send(channel: string, payload: unknown): void;
    isDestroyed(): boolean;
    on(event: 'did-navigate', listener: () => void): unknown;
    off(event: 'did-navigate', listener: () => void): unknown;
  };
  contentView: {
    addChildView(view: GuestViewLike): void;
    removeChildView(view: GuestViewLike): void;
  };
  once(event: 'closed', listener: () => void): unknown;
}

/** The slice of `ipcMain` the install needs. */
export interface GuestIpcLike {
  handle(
    channel: string,
    handler: (event: GuestIpcEvent, payload: unknown) => Promise<unknown> | unknown,
  ): void;
  removeHandler(channel: string): void;
  on(channel: string, listener: (event: GuestIpcEvent, payload: unknown) => void): unknown;
  removeListener(channel: string, listener: (event: GuestIpcEvent, payload: unknown) => void): unknown;
}

/** What an IPC event tells us about who sent it. */
export interface GuestIpcEvent {
  sender: unknown;
}

export interface GuestViewsDeps {
  /** Build a view for a managed partition. The one place privileges are
   *  decided; the page's options never reach webPreferences directly. */
  createView(options: { partition: string; userAgent?: string }): GuestViewLike;
  /** The shell's own user agent: what a desktop guest presents. */
  shellUserAgent(): string;
  /** The Chromium version a device user agent names. */
  chromeVersion: string;
  /** `session.fromPartition`, for the per-partition permission hardening. */
  sessionFor(partition: string): GuestSessionLike;
  ipc: GuestIpcLike;
  sleep(ms: number): Promise<void>;
  /** Shows a guest's right-click menu (contextMenu.ts) over the app window. */
  popupContextMenu: ContextMenuPopup;
}

export interface GuestViewsOptions {
  /** The app window. Its webContents is the only sender any handler trusts. */
  window: GuestWindowLike;
  log: LogSink;
  /** Guest devtools are a dev-build affordance; packaged builds refuse. */
  isDev: boolean;
  deps?: Partial<GuestViewsDeps>;
}

interface GuestRecord {
  guestId: string;
  view: GuestViewLike;
  /** Whether hiding also mutes — see GuestCreateOptions.muteWhenHidden. */
  muteWhenHidden: boolean;
  /** The device the guest presents as (main/guestDevices.ts). */
  device: GuestDevice;
  /** A layout batch has positioned this view at least once. Visibility is
   *  deferred until then, so a fresh view can't flash at INITIAL_BOUNDS. */
  laidOut: boolean;
  /** The page asked to show the view before its first layout arrived. */
  wantsVisibility: boolean;
  /** What the native view is actually doing right now. */
  visible: boolean;
  /** Last radius applied to this native view, in integer DIPs. */
  appliedRadius?: number;
  unregisterDownload?: () => void;
}

function realDeps(window: GuestWindowLike): GuestViewsDeps {
  return {
    createView: ({ partition, userAgent }) => {
      const view = new WebContentsView({
        webPreferences: {
          session: session.fromPartition(partition),
          // No node, no preload, no privileged anything — same posture the
          // old attach-time scrub enforced, now simply how views are born.
          sandbox: true,
          contextIsolation: true,
          nodeIntegration: false,
          nodeIntegrationInSubFrames: false,
          experimentalFeatures: false,
          webviewTag: false,
          webSecurity: true,
          allowRunningInsecureContent: false,
          // A guest navigation must never steal focus from the app window;
          // the page decides focus by clicking into the view.
          focusOnNavigation: false,
        },
      });
      if (userAgent) view.webContents.setUserAgent(userAgent);
      return view as unknown as GuestViewLike;
    },
    shellUserAgent: () => app.userAgentFallback,
    chromeVersion: process.versions.chrome,
    sessionFor: (partition) => session.fromPartition(partition),
    ipc: ipcMain,
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    // The production window is the app BrowserWindow (guestWindowOf), so a
    // guest's menu pops in the window the guest is a child of, never in
    // whichever window happens to be focused.
    popupContextMenu: popupContextMenuIn(window as unknown as BaseWindow),
  };
}

/**
 * Take ownership of the guests of one window.
 *
 * Returns a disposer, and calls it itself when the window closes — the
 * shell rebuilds the app window after a server crash, and a second install
 * over live `ipcMain.handle` registrations would throw.
 */
export function installGuestViews(options: GuestViewsOptions): () => void {
  const { window, log, isDev } = options;
  const deps: GuestViewsDeps = { ...realDeps(window), ...options.deps };
  const host = window.webContents;
  const guests = new Map<string, GuestRecord>();
  let disposed = false;

  const emit = (event: GuestEvent): void => {
    if (disposed || host.isDestroyed()) return;
    host.send(IPC.guestEvent, event);
  };

  // --- device --------------------------------------------------------------

  /** Present a guest's device screen through its CDP session: the metrics
   *  override for a phone or tablet; for desktop, none (cleared, then the
   *  session detached). Settles once the browser has the change, so a
   *  reload that follows lays out at the new screen rather than racing it. */
  const presentMetrics = async (record: GuestRecord, metrics: DeviceMetrics | null): Promise<void> => {
    const contents = record.view.webContents;
    if (contents.isDestroyed()) return;
    const session = contents.debugger;
    try {
      if (!metrics) {
        if (!session.isAttached()) return;
        try {
          await session.sendCommand('Emulation.clearDeviceMetricsOverride');
        } finally {
          if (session.isAttached()) session.detach();
        }
        return;
      }
      if (!session.isAttached()) session.attach();
      await session.sendCommand('Emulation.setDeviceMetricsOverride', metrics);
    } catch (error: unknown) {
      log.shell(`guest ${record.guestId} screen metrics: ${error instanceof Error ? error.message : String(error)}`);
    }
  };

  // --- visibility ----------------------------------------------------------

  const show = (record: GuestRecord): void => {
    record.wantsVisibility = true;
    // Deferred, not refused: the first layout applies it. Showing a view
    // that has never been positioned would paint it at INITIAL_BOUNDS
    // over whatever the page is doing.
    if (!record.laidOut) return;
    if (!record.visible) {
      record.view.setVisible(true);
      record.visible = true;
    }
    if (record.muteWhenHidden) record.view.webContents.setAudioMuted(false);
  };

  const hide = (record: GuestRecord): void => {
    record.wantsVisibility = false;
    if (record.visible) {
      record.view.setVisible(false);
      record.visible = false;
    }
    // Hidden views keep running — that is the point of hiding rather than
    // destroying — but for an ordinary page "keeps running" must not mean
    // "keeps playing audio under a screenshot".
    if (record.muteWhenHidden) record.view.webContents.setAudioMuted(true);
  };

  // --- lifecycle -----------------------------------------------------------

  const dropRecord = (record: GuestRecord): void => {
    if (guests.get(record.guestId) !== record) return;
    guests.delete(record.guestId);
    record.unregisterDownload?.();
    try {
      window.contentView.removeChildView(record.view);
    } catch {
      // The window may be mid-teardown; the view dies with it either way.
    }
    if (!record.view.webContents.isDestroyed()) {
      record.view.webContents.close({ waitForBeforeUnload: false });
    }
  };

  /**
   * Guest -> app window, so the frontend can subscribe instead of polling.
   *
   * With no DOM element in the page, this stream is the only view the
   * frontend has of a guest at all: navigation and titles for its chrome,
   * `ready` as the pool's readiness signal, `focus` and `wheel` because the
   * DOM cannot see a native view taking input, and `crashed` because only
   * this process watches a renderer die. A right-click is answered here rather
   * than forwarded: the guest gets the app window's native menu (spelling
   * suggestions, edit verbs; contextMenu.ts).
   */
  function forwardEvents(record: GuestRecord): void {
    const contents = record.view.webContents;
    const guestId = record.guestId;
    contents.on('did-navigate', (_e, url) =>
      emit({ guestId, kind: 'navigate', data: { url, inPage: false } }),
    );
    contents.on('did-navigate-in-page', (_e, url, isMainFrame) => {
      // A fragment change in some ad iframe is not a navigation anyone asked about.
      if (isMainFrame) emit({ guestId, kind: 'navigate', data: { url, inPage: true } });
    });
    contents.on('page-title-updated', (_e, title) =>
      emit({ guestId, kind: 'title', data: { title } }),
    );
    contents.on('did-start-loading', () => emit({ guestId, kind: 'loading', data: { loading: true } }));
    contents.on('did-stop-loading', () => emit({ guestId, kind: 'loading', data: { loading: false } }));
    contents.on('did-finish-load', () =>
      emit({ guestId, kind: 'ready', data: { url: contents.getURL() } }),
    );
    contents.on('focus', () => emit({ guestId, kind: 'focus', data: { focused: true } }));
    contents.on('blur', () => emit({ guestId, kind: 'focus', data: { focused: false } }));
    // Observed, never prevented: the guest keeps scrolling natively.
    contents.on('input-event', (_e, input) => {
      if (input.type !== 'mouseWheel') return;
      const wheel = input as MouseWheelInputEvent;
      emit({
        guestId,
        kind: 'wheel',
        data: {
          deltaX: domWheelDelta(wheel.deltaX),
          deltaY: domWheelDelta(wheel.deltaY),
          ctrlKey: wheel.modifiers?.includes('control') ?? false,
        },
      });
    });
    contents.on('render-process-gone', (_e, details) => {
      // The view is kept: the page decides whether to reload into it or
      // tear it down, and either verb still needs a registered guest.
      log.shell(`guest ${guestId} render process gone: ${details.reason} (${details.exitCode})`);
      emit({ guestId, kind: 'crashed', data: { reason: details.reason, exitCode: details.exitCode } });
    });
    contents.on('context-menu', contextMenuListener(contents, deps.popupContextMenu));
    contents.once('destroyed', () => {
      record.unregisterDownload?.();
      // Destroyed underneath us (window teardown, a close we issued): the
      // record is a dangling pointer now, not a guest.
      if (guests.get(guestId) === record) guests.delete(guestId);
    });
  }

  // --- the chokepoint ------------------------------------------------------

  const assertSender = (event: GuestIpcEvent): void => {
    if (event.sender !== host) {
      throw refuse('not-embedder', 'only the app window may control guests');
    }
  };

  /**
   * Resolve a page-supplied id to a guest, or refuse.
   *
   * Both checks matter. The sender check keeps a guest — or the splash —
   * from driving anything even if it learns the channel names; the
   * registry check is what stops a made-up id from reaching anything the
   * page didn't create through createGuest.
   */
  function guestFor(event: GuestIpcEvent, payload: unknown): GuestRecord {
    assertSender(event);
    const id = requireGuestId(payload);
    const record = guests.get(id);
    if (!record) {
      throw refuse('unknown-guest', `${describe(id)} is not a guest of this window`);
    }
    if (record.view.webContents.isDestroyed()) {
      guests.delete(id);
      throw refuse('guest-gone', `guest ${describe(id)} is gone`);
    }
    return record;
  }

  /**
   * An image of the guest, visible or not.
   *
   * Two real Electron bugs are worked around here, both from VS Code's
   * browser view. A hidden view that has never presented a frame gives
   * `capturePage` nothing, so visibility is flicked on and off first to
   * force the compositor to produce one (`stayHidden` keeps the capture
   * itself from re-showing it). And the capture can reject with a
   * transient viz-compositor error right after that flick, so it retries
   * a few frames before giving up.
   */
  async function captureGuest(record: GuestRecord, format: 'png' | 'placeholder'): Promise<Buffer> {
    const contents = record.view.webContents;
    for (let attempt = 1; ; attempt++) {
      try {
        if (!record.visible) {
          record.view.setVisible(true);
          record.view.setVisible(false);
        }
        const image = await contents.capturePage(
          undefined,
          record.visible ? undefined : { stayHidden: true },
        );
        if (image.isEmpty() && attempt < CAPTURE_ATTEMPTS) {
          await deps.sleep(CAPTURE_RETRY_MS);
          continue;
        }
        return format === 'placeholder' ? image.toJPEG(85) : image.toPNG();
      } catch (error) {
        const transient = error instanceof Error && /viz/i.test(error.message);
        if (!transient || attempt >= CAPTURE_ATTEMPTS) throw error;
        await deps.sleep(CAPTURE_RETRY_MS);
      }
    }
  }

  const handlers: Record<string, (event: GuestIpcEvent, payload: unknown) => Promise<unknown>> = {
    [IPC.guestCreate]: async (event, payload) => {
      assertSender(event);
      const guestId = requireGuestId(payload);
      if (guests.has(guestId)) {
        throw refuse('duplicate-guest', `${describe(guestId)} already names a guest of this window`);
      }
      const partition = field(payload, 'partition');
      if (typeof partition !== 'string' || !isManagedPartition(partition)) {
        throw refuse(
          'bad-partition',
          `partitions must be under ${PARTITION_PREFIX}, got ${describe(partition)}`,
        );
      }
      const url = field(payload, 'url');
      if (typeof url !== 'string' || !isGuestUrl(url)) {
        throw refuse('bad-url', `only http(s) URLs or ${BLANK_GUEST_URL} can be loaded, got ${describe(url)}`);
      }
      const requested = field(payload, 'device');
      const device = requested === undefined ? 'desktop' : requested;
      if (!isGuestDevice(device)) {
        throw refuse('bad-argument', `device must be desktop, phone or tablet, got ${describe(device)}`);
      }

      // A desktop guest keeps the shell's own user agent: no override.
      const presentation = devicePresentation(device, deps.shellUserAgent(), deps.chromeVersion);
      const view = deps.createView({
        partition,
        ...(device !== 'desktop' ? { userAgent: presentation.userAgent } : {}),
      });
      const record: GuestRecord = {
        guestId,
        view,
        muteWhenHidden: field(payload, 'muteWhenHidden') === true,
        device,
        laidOut: false,
        wantsVisibility: false,
        visible: false,
      };
      guests.set(guestId, record);

      const guestSession = deps.sessionFor(partition);
      denyPermissions(guestSession);
      record.unregisterDownload = guestDownloads.register(guestSession, view.webContents, (data) =>
        emit({ guestId, kind: 'download', data }),
      );
      // Guests get no popups, full stop: `window.open` and `target=_blank`
      // are denied and dropped. If a real need appears it comes back as a
      // deliberate decision, not a default.
      view.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      forwardEvents(record);
      // The metrics override does not hold across every new document (one
      // sent before the first navigation is dropped): every main-frame
      // navigation re-applies the device's.
      view.webContents.on('did-navigate', () => {
        if (record.device !== 'desktop') void presentMetrics(record, deviceMetrics(record.device));
      });

      window.contentView.addChildView(view);
      view.setBounds(INITIAL_BOUNDS);
      view.setVisible(false);

      // Not awaited: the page learns about the load through the event
      // stream, and createGuest resolving means "the guest exists", not
      // "the page is up". Load failures surface as loading/navigate
      // events, never as an unhandled rejection here.
      view.webContents.loadURL(url).catch(() => undefined);

      log.shell(`guest ${guestId} created (${partition})`);
      const state: GuestState = { guestId, url };
      return state;
    },

    [IPC.guestDestroy]: async (event, payload) => {
      assertSender(event);
      const id = requireGuestId(payload);
      // Idempotent by design: teardown races (a destroy racing the window
      // rebuild, a double destroy from an unmount) should converge on
      // "gone", not on an error nobody can act on.
      const record = guests.get(id);
      if (!record) return;
      log.shell(`guest ${id} destroyed`);
      dropRecord(record);
    },

    [IPC.guestSetVisible]: async (event, payload) => {
      const record = guestFor(event, payload);
      const visible = field(payload, 'visible');
      if (typeof visible !== 'boolean') {
        throw refuse('bad-argument', `visible must be a boolean, got ${describe(visible)}`);
      }
      if (visible) show(record);
      else hide(record);
    },

    [IPC.guestRaise]: async (event, payload) => {
      const record = guestFor(event, payload);
      // Re-adding an attached child moves it to the end of the child list,
      // which is the top of the native z-order — the one ordering verb a
      // view has.
      window.contentView.addChildView(record.view);
    },

    [IPC.guestEvaluate]: async (event, payload) => {
      const record = guestFor(event, payload);
      const js = field(payload, 'js');
      if (typeof js !== 'string') throw refuse('bad-argument', `js must be a string, got ${describe(js)}`);
      // userGesture: the page is scripting a view the user is looking at,
      // and half of what a generator renders needs one to run at all.
      return record.view.webContents.executeJavaScript(js, true);
    },

    [IPC.guestCapturePng]: async (event, payload) => {
      const record = guestFor(event, payload);
      const png = await captureGuest(record, 'png');
      // Node pools Buffer storage, so `png.buffer` is usually a slab with
      // other people's bytes in it. Copy out before it crosses the wire.
      return new Uint8Array(png).buffer;
    },

    // Private renderer-host presentation path. Never use these lossy bytes
    // for the SDK screenshot RPC or a saved screenshot.
    [IPC.guestCapturePlaceholder]: async (event, payload) => {
      const record = guestFor(event, payload);
      const jpeg = await captureGuest(record, 'placeholder');
      return new Uint8Array(jpeg).buffer;
    },

    [IPC.guestCurrentUrl]: async (event, payload) => guestFor(event, payload).view.webContents.getURL(),

    [IPC.guestNavigate]: async (event, payload) => {
      const record = guestFor(event, payload);
      const url = field(payload, 'url');
      if (typeof url !== 'string' || !isGuestUrl(url)) {
        throw refuse('bad-url', `only http(s) URLs or ${BLANK_GUEST_URL} can be loaded, got ${describe(url)}`);
      }
      try {
        await record.view.webContents.loadURL(url);
      } catch (error) {
        const failure = error as { code?: unknown; errno?: unknown } | null;
        if (failure?.code !== 'ERR_ABORTED' && failure?.errno !== -3) throw error;
      }
    },

    [IPC.guestGoBack]: async (event, payload) => {
      const history = guestFor(event, payload).view.webContents.navigationHistory;
      if (!history.canGoBack()) return false;
      history.goBack();
      return true;
    },

    [IPC.guestGoForward]: async (event, payload) => {
      const history = guestFor(event, payload).view.webContents.navigationHistory;
      if (!history.canGoForward()) return false;
      history.goForward();
      return true;
    },

    [IPC.guestReload]: async (event, payload) => {
      guestFor(event, payload).view.webContents.reload();
    },

    [IPC.guestSetDevice]: async (event, payload) => {
      const record = guestFor(event, payload);
      const device = field(payload, 'device');
      if (!isGuestDevice(device)) {
        throw refuse('bad-argument', `device must be desktop, phone or tablet, got ${describe(device)}`);
      }
      const contents = record.view.webContents;
      const presentation = devicePresentation(device, deps.shellUserAgent(), deps.chromeVersion);
      record.device = device;
      contents.setUserAgent(presentation.userAgent);
      await presentMetrics(record, presentation.metrics);
      // In place: same view, same partition, same history. The reload is
      // what makes the site see the new user agent (and, once the metrics
      // above have settled, the new screen).
      contents.reload();
      log.shell(`guest ${record.guestId} presents as ${device}`);
    },

    [IPC.guestOpenDevTools]: async (event, payload) => {
      // Checked before anything else: a packaged build has no answer to
      // this question at all, whatever the id turns out to be.
      if (!isDev) throw refuse('devtools-unavailable', 'guest devtools are a dev-build affordance');
      guestFor(event, payload).view.webContents.openDevTools({ mode: 'detach' });
    },

    [IPC.clearPartition]: async (event, payload) => {
      assertSender(event);
      const partition = field(payload, 'partition');
      if (typeof partition !== 'string' || !isManagedPartition(partition)) {
        throw refuse('bad-partition', `only ${PARTITION_PREFIX}* partitions can be cleared, got ${describe(partition)}`);
      }
      log.shell(`clearing partition ${partition}`);
      await deps.sessionFor(partition).clearStorageData();
    },
  };

  // --- the layout hot path -------------------------------------------------

  /**
   * Fire-and-forget on purpose: bounds ride behind the DOM as it is, and
   * an invoke round-trip per pass would only stretch that. Bad payloads
   * and unknown ids are silently dropped — there is nobody to answer.
   *
   * The renderer pre-snaps every value so the rounding here is an
   * identity; see GuestLayoutRect in shared/bridge.ts.
   */
  const onLayout = (event: GuestIpcEvent, payload: unknown): void => {
    if (disposed || event.sender !== host) return;
    const batch = field(payload, 'guests');
    const zoomRaw = field(payload, 'zoomFactor');
    const zoom = typeof zoomRaw === 'number' && Number.isFinite(zoomRaw) && zoomRaw > 0 ? zoomRaw : 1;
    if (!Array.isArray(batch)) return;
    for (const item of batch) {
      const guestId = field(item, 'guestId');
      if (typeof guestId !== 'string') continue;
      const record = guests.get(guestId);
      if (!record || record.view.webContents.isDestroyed()) continue;
      const x = field(item, 'x');
      const y = field(item, 'y');
      const w = field(item, 'w');
      const h = field(item, 'h');
      if (
        typeof x !== 'number' || typeof y !== 'number' ||
        typeof w !== 'number' || typeof h !== 'number' ||
        !Number.isFinite(x) || !Number.isFinite(y) ||
        !Number.isFinite(w) || !Number.isFinite(h)
      ) {
        continue;
      }
      record.view.setBounds({
        x: Math.round(x * zoom),
        y: Math.round(y * zoom),
        width: Math.max(0, Math.round(w * zoom)),
        height: Math.max(0, Math.round(h * zoom)),
      });
      const radius = field(item, 'radius');
      if (typeof radius === 'number' && Number.isFinite(radius) && radius >= 0) {
        const nativeRadius = Math.round(radius * zoom);
        if (record.appliedRadius !== nativeRadius) {
          record.view.setBorderRadius(nativeRadius);
          record.appliedRadius = nativeRadius;
        }
      }
      if (!record.laidOut) {
        record.laidOut = true;
        // The anti-flash deferral: a show() that arrived before this
        // layout lands now, on real bounds.
        if (record.wantsVisibility) show(record);
      }
    }
  };

  // --- install / dispose ---------------------------------------------------

  /**
   * A main-frame navigation of the APP WINDOW (a reload, above all) ends
   * the page that created these guests. Its successor knows nothing of
   * them — the ids were minted by the old page's pool — so an orphaned
   * guest here would be an invisible renderer process nothing can ever
   * address again. Reap them all; the fresh page creates its own. (The
   * window's very first load fires this too, over an empty registry.)
   * SPA route changes are in-page navigations and don't land here.
   */
  const onHostNavigated = (): void => {
    if (disposed || guests.size === 0) return;
    log.shell(`app window navigated; reclaiming ${guests.size} orphaned guest(s)`);
    for (const record of Array.from(guests.values())) dropRecord(record);
    guests.clear();
  };

  for (const [channel, handler] of Object.entries(handlers)) {
    deps.ipc.handle(channel, handler);
  }
  deps.ipc.on(IPC.guestLayout, onLayout);
  host.on('did-navigate', onHostNavigated);

  const dispose = (): void => {
    if (disposed) return;
    disposed = true;
    for (const channel of Object.keys(handlers)) deps.ipc.removeHandler(channel);
    deps.ipc.removeListener(IPC.guestLayout, onLayout);
    if (!host.isDestroyed()) host.off('did-navigate', onHostNavigated);
    // The window owns the views natively, but a dispose that isn't a
    // window close (the explicit disposer) must not leak renderers.
    for (const record of Array.from(guests.values())) dropRecord(record);
    guests.clear();
  };

  window.once('closed', dispose);
  return dispose;
}

/**
 * Adapt the real app window to the slice the manager drives.
 *
 * A `BrowserWindow` satisfies GuestWindowLike structurally, but its
 * overloaded event methods don't assign cleanly to the narrowed `once`;
 * this is the single boundary where that is papered over.
 */
export function guestWindowOf(window: BrowserWindow): GuestWindowLike {
  return window as unknown as GuestWindowLike;
}

/**
 * Deny-by-default permissions for a guest partition.
 *
 * Installed once per `Session` — the handlers are session-wide and the
 * last one set wins, so re-installing per guest would be pointless churn.
 * Only ever called with a guest's session: createGuest's partition gate
 * keeps guests out of the app's own session, so this can't tighten the
 * app window's permissions by accident.
 */
function denyPermissions(target: GuestSessionLike): void {
  if (hardenedSessions.has(target)) return;
  hardenedSessions.add(target);
  // Both halves are needed: the request handler answers `getUserMedia()`
  // and friends, the check handler answers the `permissions.query()` a
  // page uses to decide whether to bother asking.
  target.setPermissionRequestHandler((_contents, permission, callback) => {
    callback(GUEST_PERMISSIONS.has(permission));
  });
  target.setPermissionCheckHandler((_contents, permission) => GUEST_PERMISSIONS.has(permission));
}

/** True for a partition this shell is willing to manage. */
export function isManagedPartition(partition: string): boolean {
  return partition.startsWith(PARTITION_PREFIX) && partition.length > PARTITION_PREFIX.length;
}

/** True for a plain web URL. Kept http(s)-only: it is the predicate the
 *  guest gate is built from, and nothing else may widen through it. */
export function isWebUrl(url: string): boolean {
  try {
    const { protocol } = new URL(url);
    return protocol === 'http:' || protocol === 'https:';
  } catch {
    return false;
  }
}

/**
 * The empty page. A guest legitimately starts here — a web session opened
 * with no site of its own is the blank page plus an address bar — and it
 * is the one non-http(s) URL that names nothing: no file, no host, no
 * scheme handler. It is not what the gate below exists to stop.
 */
export const BLANK_GUEST_URL = 'about:blank';

/** True for a URL a guest may be pointed at: the web, or nothing at all.
 *  Every other scheme (`file:`, `smb:`, `javascript:`, an OS handler) is
 *  page content asking this process to open something, and is refused. */
export function isGuestUrl(url: string): boolean {
  return url === BLANK_GUEST_URL || isWebUrl(url);
}

function field(payload: unknown, key: string): unknown {
  if (typeof payload !== 'object' || payload === null) return undefined;
  return (payload as Record<string, unknown>)[key];
}

/** The payload's guestId, refused unless it is a non-empty string of at most 256 characters. */
function requireGuestId(payload: unknown): string {
  const id = field(payload, 'guestId');
  if (typeof id !== 'string' || id.length === 0 || id.length > 256) {
    throw refuse('bad-argument', `guestId must be a non-empty string of at most 256 characters, got ${describe(id)}`);
  }
  return id;
}

/** A value in an error message, without pasting page content into the log. */
function describe(value: unknown): string {
  if (typeof value === 'string') return value.length > 60 ? `a ${value.length}-character string` : JSON.stringify(value);
  return value === null ? 'null' : typeof value;
}
