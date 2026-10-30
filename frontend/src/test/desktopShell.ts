// A stand-in for `window.collimateDesktop`.
//
// Every guest op is a spy, and `onGuestEvent` is a real subscription list
// the test drives with `emitGuestEvent` — which is what lets a test assert
// that a handle routes only its own guest's events, and that destroying it
// actually unsubscribes. `createGuest` records what the page asked for and
// `layoutBatches` records every layout send, so pool tests can assert the
// whole geometry stream without a shell.

import { vi } from "vitest";
import type {
  CollimateDesktop,
  DesktopCapabilities,
  GuestEvent,
  GuestLayoutBatch,
  GuestOps,
  GuestState,
  ServerEntry,
  ServersOps,
  ShellCommand,
  TitleBarOps,
} from "@/lib/desktop";

export const ALL_DESKTOP_CAPS: DesktopCapabilities = {
  nativeGuests: true,
  guestCapture: true,
  persistentPartitions: true,
  directLoopback: true,
  externalOpen: true,
  customTitleBar: true,
  customWindowControls: true,
};

export interface DesktopShellStub {
  /** The object installed on `window.collimateDesktop`. */
  shell: CollimateDesktop;
  /** Deliver an event to every live `onGuestEvent` subscriber. */
  emitGuestEvent: (event: GuestEvent) => void;
  /** Live `onGuestEvent` subscriptions; 0 once every handle is destroyed. */
  subscriberCount: () => number;
  /** Every `layoutGuests` batch, in send order. */
  layoutBatches: GuestLayoutBatch[];
  setZoomFactor: (factor: number) => void;
  /** Deliver a registry snapshot to every live `onServersChanged` subscriber. */
  emitServersChanged: (entries: ServerEntry[]) => void;
  /** Deliver a shell menu command to every live `onShellCommand` subscriber. */
  emitShellCommand: (command: ShellCommand) => void;
  /** Live `onServersChanged` subscriptions. */
  serversSubscriberCount: () => number;
}

/** The registry's view of the stub's own server: local, running, the
 *  window's server. */
export const LOCAL_SERVER_ENTRY: ServerEntry = {
  id: "local",
  kind: "local",
  name: "Local",
  url: "http://127.0.0.1:8765",
  status: "running",
  serverVersion: "0.4.1",
  hasToken: false,
  channels: [],
  activeChannelId: null,
  isWindowServer: true,
};

export function installDesktopShell(
  overrides: Partial<CollimateDesktop> = {},
): DesktopShellStub {
  const subscribers = new Set<(event: GuestEvent) => void>();
  const layoutBatches: GuestLayoutBatch[] = [];
  const zoomSubscribers = new Set<(factor: number) => void>();
  const serversSubscribers = new Set<(entries: ServerEntry[]) => void>();
  const commandSubscribers = new Set<(command: ShellCommand) => void>();
  let zoomFactor = 1;

  const guest: GuestOps = {
    createGuest: vi.fn(async (options): Promise<GuestState> => ({
      guestId: options.guestId,
      url: options.url,
    })),
    destroyGuest: vi.fn(async () => {}),
    setGuestVisible: vi.fn(async () => {}),
    raiseGuest: vi.fn(async () => {}),
    layoutGuests: vi.fn((batch: GuestLayoutBatch) => {
      layoutBatches.push(batch);
    }),
    evaluate: vi.fn(async () => undefined),
    capturePlaceholder: vi.fn(async () => new ArrayBuffer(0)),
    capturePng: vi.fn(async () => new ArrayBuffer(8)),
    currentUrl: vi.fn(async () => "https://example.test/"),
    navigate: vi.fn(async () => {}),
    goBack: vi.fn(async () => true),
    goForward: vi.fn(async () => false),
    reload: vi.fn(async () => {}),
    setGuestDevice: vi.fn(async () => {}),
    openDevTools: vi.fn(async () => {}),
    clearPartition: vi.fn(async () => {}),
  };

  const titleBar: TitleBarOps = {
    menus: vi.fn(async () => [{ label: "File" }, { label: "Help" }]),
    popupMenu: vi.fn(async () => {}),
    toggleMaximize: vi.fn(async () => true),
    minimize: vi.fn(async () => {}),
    close: vi.fn(async () => {}),
    isMaximized: vi.fn(async () => false),
  };

  const servers: ServersOps = {
    list: vi.fn(async () => [{ ...LOCAL_SERVER_ENTRY }]),
    add: vi.fn(async (draft) => ({
      ...LOCAL_SERVER_ENTRY,
      id: "added",
      kind: "remote" as const,
      name: draft.name ?? draft.url,
      url: draft.url,
      hasToken: !!draft.token,
      isWindowServer: false,
    })),
    update: vi.fn(async (patch) => ({ ...LOCAL_SERVER_ENTRY, ...patch })),
    remove: vi.fn(async () => {}),
    probe: vi.fn(async () => ({ ok: true as const, serverVersion: "0.4.1" })),
    openServer: vi.fn(async () => {}),
    openChannel: vi.fn(async () => {}),
    startLocal: vi.fn(async () => {}),
    stopLocal: vi.fn(async () => {}),
    getPrefs: vi.fn(async () => ({ keepLocalRunning: false })),
    setPrefs: vi.fn(async (patch) => ({ keepLocalRunning: false, ...patch })),
  };

  const shell: CollimateDesktop = {
    shellVersion: "0.4.1",
    bridge: 6,
    platform: "darwin",
    capabilities: { ...ALL_DESKTOP_CAPS },
    server: {
      id: "local",
      kind: "local",
      name: "Local",
      url: "http://127.0.0.1:8765",
      port: 8765,
      dataDir: "/tmp/collimate",
      serverVersion: "0.4.1",
    },
    servers,
    onServersChanged: (handler) => {
      serversSubscribers.add(handler);
      return () => { serversSubscribers.delete(handler); };
    },
    onShellCommand: (handler) => {
      commandSubscribers.add(handler);
      return () => { commandSubscribers.delete(handler); };
    },
    openExternal: vi.fn(async () => true),
    setWindowTheme: vi.fn(),
    getZoomFactor: () => zoomFactor,
    onZoomChanged: (handler) => {
      zoomSubscribers.add(handler);
      handler(zoomFactor);
      return () => { zoomSubscribers.delete(handler); };
    },
    guest,
    titleBar,
    onGuestEvent: (handler) => {
      subscribers.add(handler);
      return () => {
        subscribers.delete(handler);
      };
    },
    ...overrides,
  };

  window.collimateDesktop = shell;

  return {
    shell,
    emitGuestEvent: (event) => {
      for (const handler of Array.from(subscribers)) handler(event);
    },
    subscriberCount: () => subscribers.size,
    layoutBatches,
    setZoomFactor: (factor) => {
      zoomFactor = factor;
      for (const handler of zoomSubscribers) handler(factor);
    },
    emitServersChanged: (entries) => {
      for (const handler of Array.from(serversSubscribers)) handler(entries);
    },
    emitShellCommand: (command) => {
      for (const handler of Array.from(commandSubscribers)) handler(command);
    },
    serversSubscriberCount: () => serversSubscribers.size,
  };
}

export function uninstallDesktopShell(): void {
  delete window.collimateDesktop;
}
