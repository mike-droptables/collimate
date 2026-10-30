import { contextBridge, ipcRenderer, webFrame, type IpcRendererEvent } from 'electron';
import {
  ARG_BOOTSTRAP,
  ARG_SURFACE,
  BRIDGE_PROTOCOL,
  DEFAULT_INTERFACE_SCALE,
  IPC,
  isInterfaceScale,
  type CollimateDesktop,
  type CollimateSplash,
  type GuestCreateOptions,
  type GuestDevice,
  type GuestEvent,
  type GuestLayoutBatch,
  type GuestOps,
  type GuestState,
  type RendererBootstrap,
  type ServerDraft,
  type ServerEntry,
  type ServerProbeResult,
  type ServersOps,
  type ServersReply,
  type ShellCommand,
  type ShellPrefs,
  type SplashEvent,
  type Surface,
  type TitleBarMenu,
  type TitleBarOps,
  type WindowTheme,
} from '../shared/bridge.js';

/**
 * The preload — the only code that spans the shell and a renderer.
 *
 * One bundle serves both surfaces (the app window and the splash); which
 * one it is arrives as a `webPreferences.additionalArguments` flag. The
 * app window's server details arrive the same way, base64 in argv, rather
 * than over IPC: that makes `window.collimateDesktop` complete before the
 * page's first script runs, so the frontend never has to handle a
 * half-initialised bridge.
 *
 * What it exposes stays thin on purpose: identity, capabilities, opening a
 * link in the real browser, the native folder dialog, repainting the
 * window in the page's skin, a set of `invoke` calls for driving guest
 * views, and the servers registry.
 * No decisions are made here — every guest and servers call is authorised
 * in the main process (see main/guestViews.ts, main/index.ts), because this script runs in
 * the same process as the page and is no more trustworthy than it.
 * Everything must stay capability-gated so a plain browser tab keeps
 * working.
 */

function argValue(prefix: string): string | null {
  for (const arg of process.argv) {
    if (arg.startsWith(prefix)) return arg.slice(prefix.length);
  }
  return null;
}

function readSurface(): Surface {
  return argValue(ARG_SURFACE) === 'splash' ? 'splash' : 'main';
}

function readBootstrap(): RendererBootstrap | null {
  const encoded = argValue(ARG_BOOTSTRAP);
  if (!encoded) return null;
  try {
    // Web APIs, not Buffer: Electron 45 gives a sandboxed preload no Buffer.
    const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0));
    const parsed: unknown = JSON.parse(new TextDecoder().decode(bytes));
    if (typeof parsed !== 'object' || parsed === null) return null;
    return parsed as RendererBootstrap;
  } catch {
    return null;
  }
}

/**
 * `guestId` is the string the page minted and passed to `createGuest`.
 * Passing one the shell didn't create rejects — see main/guestViews.ts.
 */
const guest: GuestOps = {
  async createGuest(options: GuestCreateOptions): Promise<GuestState> {
    const state: unknown = await ipcRenderer.invoke(IPC.guestCreate, options);
    if (typeof state !== 'object' || state === null) {
      throw new Error('createGuest: the shell returned no guest');
    }
    return state as GuestState;
  },
  async destroyGuest(guestId: string): Promise<void> {
    await ipcRenderer.invoke(IPC.guestDestroy, { guestId });
  },
  async setGuestVisible(guestId: string, visible: boolean): Promise<void> {
    await ipcRenderer.invoke(IPC.guestSetVisible, { guestId, visible });
  },
  async raiseGuest(guestId: string): Promise<void> {
    await ipcRenderer.invoke(IPC.guestRaise, { guestId });
  },
  layoutGuests(batch: GuestLayoutBatch): void {
    // send, never invoke: this is the per-frame hot path, and nothing
    // useful could come back anyway.
    ipcRenderer.send(IPC.guestLayout, batch);
  },
  async evaluate(guestId: string, js: string): Promise<unknown> {
    return ipcRenderer.invoke(IPC.guestEvaluate, { guestId, js });
  },
  async capturePng(guestId: string): Promise<ArrayBuffer> {
    const bytes: unknown = await ipcRenderer.invoke(IPC.guestCapturePng, { guestId });
    if (!(bytes instanceof ArrayBuffer)) throw new Error('capturePng: the shell returned no image');
    return bytes;
  },
  async capturePlaceholder(guestId: string): Promise<ArrayBuffer> {
    const bytes: unknown = await ipcRenderer.invoke(IPC.guestCapturePlaceholder, { guestId });
    if (!(bytes instanceof ArrayBuffer)) throw new Error('capturePlaceholder: the shell returned no image');
    return bytes;
  },
  async currentUrl(guestId: string): Promise<string> {
    const url: unknown = await ipcRenderer.invoke(IPC.guestCurrentUrl, { guestId });
    return typeof url === 'string' ? url : '';
  },
  async navigate(guestId: string, url: string): Promise<void> {
    await ipcRenderer.invoke(IPC.guestNavigate, { guestId, url });
  },
  async goBack(guestId: string): Promise<boolean> {
    return (await ipcRenderer.invoke(IPC.guestGoBack, { guestId })) === true;
  },
  async goForward(guestId: string): Promise<boolean> {
    return (await ipcRenderer.invoke(IPC.guestGoForward, { guestId })) === true;
  },
  async reload(guestId: string): Promise<void> {
    await ipcRenderer.invoke(IPC.guestReload, { guestId });
  },
  async setGuestDevice(guestId: string, device: GuestDevice): Promise<void> {
    await ipcRenderer.invoke(IPC.guestSetDevice, { guestId, device });
  },
  async openDevTools(guestId: string): Promise<void> {
    await ipcRenderer.invoke(IPC.guestOpenDevTools, { guestId });
  },
  async clearPartition(partition: string): Promise<void> {
    await ipcRenderer.invoke(IPC.clearPartition, { partition });
  },
};

/** Page-drawn title bar verbs — see main/titleBar.ts for the answering side. */
const titleBar: TitleBarOps = {
  async menus(): Promise<TitleBarMenu[]> {
    const entries: unknown = await ipcRenderer.invoke(IPC.titleBarMenus);
    if (!Array.isArray(entries)) return [];
    return entries.filter(
      (entry): entry is TitleBarMenu =>
        typeof entry === 'object' && entry !== null && typeof (entry as TitleBarMenu).label === 'string',
    );
  },
  async popupMenu(index: number, x: number, y: number): Promise<void> {
    await ipcRenderer.invoke(IPC.titleBarPopupMenu, { index, x, y });
  },
  async toggleMaximize(): Promise<boolean> {
    return (await ipcRenderer.invoke(IPC.titleBarToggleMaximize)) === true;
  },
  async minimize(): Promise<void> {
    await ipcRenderer.invoke(IPC.titleBarMinimize);
  },
  async close(): Promise<void> {
    await ipcRenderer.invoke(IPC.titleBarClose);
  },
  async isMaximized(): Promise<boolean> {
    return (await ipcRenderer.invoke(IPC.titleBarIsMaximized)) === true;
  },
};

/**
 * One servers/prefs invoke: main answers `{ value }` or `{ error }`, and an
 * error becomes a rejection carrying the shell's own sentence.
 */
async function serversCall<T>(channel: string, payload?: unknown): Promise<T> {
  const reply = (await ipcRenderer.invoke(channel, payload)) as ServersReply<T> | null | undefined;
  if (reply && typeof reply === 'object' && 'error' in reply) throw new Error(String(reply.error));
  if (!reply || typeof reply !== 'object' || !('value' in reply)) {
    throw new Error(`${channel}: the shell gave no answer`);
  }
  return reply.value;
}

/** The servers registry — see main/servers.ts for the answering side. */
const servers: ServersOps = {
  async list(): Promise<ServerEntry[]> {
    const entries = await serversCall<unknown>(IPC.serversList);
    return Array.isArray(entries) ? (entries as ServerEntry[]) : [];
  },
  add: (draft: ServerDraft) => serversCall<ServerEntry>(IPC.serversAdd, draft),
  update: (patch) => serversCall<ServerEntry>(IPC.serversUpdate, patch),
  async remove(id: string): Promise<void> {
    await serversCall<null>(IPC.serversRemove, { id });
  },
  probe: (candidate) => serversCall<ServerProbeResult>(IPC.serversProbe, candidate),
  async openServer(id: string): Promise<void> {
    await serversCall<null>(IPC.serversOpen, { id });
  },
  async openChannel(id: string, channelId: string): Promise<void> {
    await serversCall<null>(IPC.serversOpenChannel, { id, channelId });
  },
  async startLocal(): Promise<void> {
    await serversCall<null>(IPC.serversStartLocal);
  },
  async stopLocal(): Promise<void> {
    await serversCall<null>(IPC.serversStopLocal);
  },
  getPrefs: () => serversCall<ShellPrefs>(IPC.shellPrefsGet),
  setPrefs: (patch: Partial<ShellPrefs>) => serversCall<ShellPrefs>(IPC.shellPrefsSet, patch),
};

/**
 * Subscribe through one ipcRenderer listener per channel, fanned out to
 * every handler: many subscribers do not stack emitter listeners
 * (MaxListenersExceeded), and one throwing handler does not stop delivery
 * to the rest. Returns the subscribe function.
 */
function fanOut<T>(channel: string, accept: (payload: unknown) => payload is T): (handler: (payload: T) => void) => () => void {
  const handlers = new Set<(payload: T) => void>();
  let wired = false;
  return (handler) => {
    if (!wired) {
      wired = true;
      ipcRenderer.on(channel, (_event: IpcRendererEvent, payload: unknown) => {
        if (!accept(payload)) return;
        for (const h of Array.from(handlers)) {
          try {
            h(payload);
          } catch (err) {
            console.error(`${channel} handler error:`, err);
          }
        }
      });
    }
    // Wrap so the same handler subscribed twice gets two subscriptions.
    const subscription = (payload: T): void => handler(payload);
    handlers.add(subscription);
    return () => {
      handlers.delete(subscription);
    };
  };
}

const onServersChanged = fanOut<ServerEntry[]>(IPC.serversChanged, (payload): payload is ServerEntry[] =>
  Array.isArray(payload),
);
const onShellCommand = fanOut<ShellCommand>(
  IPC.shellCommand,
  (payload): payload is ShellCommand =>
    typeof payload === 'object' && payload !== null && (
      (payload as ShellCommand).type === 'openServers'
      || ((payload as ShellCommand).type === 'localStopFailed'
        && typeof (payload as { message?: unknown }).message === 'string')
    ),
);

// One ipcRenderer listener fans guest events out to every subscriber, so
// many guest mounts do not stack emitter listeners (MaxListenersExceeded) and
// one throwing subscriber does not stop delivery to the rest.
const guestEventHandlers = new Set<(event: GuestEvent) => void>();
let guestEventWired = false;

function exposeMain(bootstrap: RendererBootstrap): void {
  // The interface scale starts from the bootstrap (the window's settings
  // when it opened) and follows what main reports it applied: after every
  // load (the argv bootstrap of a reloaded page can be stale) and after a
  // View-menu step.
  let interfaceScale: number = isInterfaceScale(bootstrap.interfaceScale)
    ? bootstrap.interfaceScale
    : DEFAULT_INTERFACE_SCALE;
  ipcRenderer.on(IPC.interfaceScale, (_event: IpcRendererEvent, scale: unknown) => {
    if (isInterfaceScale(scale)) interfaceScale = scale;
  });
  const desktop: CollimateDesktop = {
    shellVersion: bootstrap.shellVersion,
    bridge: BRIDGE_PROTOCOL,
    platform: process.platform,
    capabilities: bootstrap.capabilities,
    server: bootstrap.server,
    async openExternal(url: string): Promise<boolean> {
      if (typeof url !== 'string') return false;
      const result: unknown = await ipcRenderer.invoke(IPC.openExternal, url);
      return result === true;
    },
    async pickFolder(): Promise<string | null> {
      const picked: unknown = await ipcRenderer.invoke(IPC.pickFolder);
      return typeof picked === 'string' ? picked : null;
    },
    getZoomFactor: () => webFrame.getZoomFactor(),
    onZoomChanged(handler: (factor: number) => void): () => void {
      let previous = webFrame.getZoomFactor();
      const changed = (): void => {
        const factor = webFrame.getZoomFactor();
        if (factor === previous) return;
        previous = factor;
        handler(factor);
      };
      // Page zoom changes the CSS viewport, whether it came from the menu,
      // an accelerator or the host. No polling and no guest-controlled IPC.
      window.addEventListener('resize', changed);
      window.visualViewport?.addEventListener('resize', changed);
      handler(previous);
      return () => {
        window.removeEventListener('resize', changed);
        window.visualViewport?.removeEventListener('resize', changed);
      };
    },
    getInterfaceScale: () => interfaceScale,
    setInterfaceScale(factor: number): void {
      // send, never invoke, like setWindowTheme: main validates, applies
      // and persists, then reports the applied scale back.
      if (!isInterfaceScale(factor)) return;
      interfaceScale = factor;
      ipcRenderer.send(IPC.interfaceScale, factor);
    },
    setWindowTheme(theme: WindowTheme): void {
      // send, never invoke: the page has nothing to wait for, and main
      // validates the colours (see main/titleBar.ts).
      ipcRenderer.send(IPC.windowTheme, theme);
    },
    guest,
    titleBar,
    onGuestEvent(handler: (event: GuestEvent) => void): () => void {
      if (!guestEventWired) {
        guestEventWired = true;
        ipcRenderer.on(IPC.guestEvent, (_event: IpcRendererEvent, payload: GuestEvent) => {
          for (const h of Array.from(guestEventHandlers)) {
            try {
              h(payload);
            } catch (err) {
              console.error('guest event handler error:', err);
            }
          }
        });
      }
      // Wrap so the same handler subscribed twice gets two subscriptions.
      const subscription = (event: GuestEvent): void => handler(event);
      guestEventHandlers.add(subscription);
      return () => {
        guestEventHandlers.delete(subscription);
      };
    },
    servers,
    onServersChanged,
    onShellCommand,
  };
  contextBridge.exposeInMainWorld('collimateDesktop', desktop);
}

function exposeSplash(): void {
  const splash: CollimateSplash = {
    onEvent(handler: (event: SplashEvent) => void): void {
      ipcRenderer.on(IPC.splashEvent, (_event, payload: SplashEvent) => handler(payload));
    },
    send(action): void {
      ipcRenderer.send(IPC.splashAction, action);
    },
  };
  contextBridge.exposeInMainWorld('collimateSplash', splash);
}

if (readSurface() === 'splash') {
  exposeSplash();
} else {
  const bootstrap = readBootstrap();
  // No bootstrap means we can't honestly describe the shell to the page.
  // Exposing nothing is the right failure: the frontend already has to
  // work without `window.collimateDesktop` (that's a browser tab).
  if (bootstrap) exposeMain(bootstrap);
}
