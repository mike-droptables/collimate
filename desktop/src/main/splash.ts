import { BrowserWindow, ipcMain, type IpcMainEvent } from 'electron';
import path from 'node:path';
import { ARG_SURFACE, IPC, type SplashAction, type SplashEvent, type SplashView } from '../shared/bridge.js';
import { DEFAULT_WINDOW_TITLE, NIGHT_WINDOW_THEME, pinWindowTitle } from './window.js';

/**
 * The pre-window surface: startup progress, failures, and the connect
 * form.
 *
 * This is the only `file://` page the shell ever loads. Everything else
 * comes from the server — which is the point of the architecture, so the
 * exception is kept to one small, self-contained, network-free document.
 */

const WIDTH = 520;
/** Each view gets the height its content actually needs. */
const HEIGHTS = { status: 300, connect: 500, failure: 470 } as const;

export interface SplashOptions {
  onAction: (action: SplashAction) => void;
  /**
   * Window title, from the app profile (see main/profile.ts). The splash is
   * the app's first taskbar entry, so a dev run has to be named here too or
   * the first thing on screen claims to be the installed app. Defaults to
   * the name the stable app has always used.
   */
  title?: string;
}

export class Splash {
  private window: BrowserWindow | null = null;
  private loaded = false;
  private queue: SplashEvent[] = [];
  private view: SplashView = { kind: 'status', message: 'Starting Collimate…' };
  private revision = 0;

  constructor(private readonly options: SplashOptions) {
    ipcMain.on(IPC.splashAction, this.handleAction);
  }

  /** A late startup callback cannot dismiss a newer form on the same window. */
  get viewRevision(): number {
    return this.revision;
  }

  get isOpen(): boolean {
    return this.window !== null && !this.window.isDestroyed();
  }

  show(): BrowserWindow {
    if (this.window && !this.window.isDestroyed()) {
      this.raise(this.window);
      return this.window;
    }

    const title = this.options.title ?? DEFAULT_WINDOW_TITLE;
    const window = new BrowserWindow({
      width: WIDTH,
      height: HEIGHTS.status,
      resizable: false,
      frame: false,
      // A splash that can end up behind the browser you were reading the
      // install instructions in is a support ticket.
      alwaysOnTop: true,
      center: true,
      show: false,
      backgroundColor: NIGHT_WINDOW_THEME.background,
      title,
      // Same window/taskbar icon as the main window (see window.ts) — the
      // splash is the app's first taskbar entry, so it must not flash the
      // stock Electron icon before the real window takes over.
      icon: path.join(__dirname, 'app-icon.png'),
      webPreferences: {
        preload: path.join(__dirname, 'preload.cjs'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: false,
        additionalArguments: [`${ARG_SURFACE}splash`],
      },
    });

    this.loaded = false;
    this.window = window;

    // The constructor's `title` alone is not enough here: splash.html
    // declares <title>Collimate</title>, and Electron renames the window to
    // the document's title the moment the page loads. Same handler, same
    // reason, as the main window — see pinWindowTitle in main/window.ts.
    pinWindowTitle(window, title);

    window.once('ready-to-show', () => window.show());
    window.webContents.on('did-finish-load', () => {
      this.loaded = true;
      // Replay whatever happened while the document was loading; startup
      // is fast enough that the first few status lines usually land here.
      this.post({ type: 'view', view: this.view });
      const queued = this.queue;
      this.queue = [];
      for (const event of queued) this.post(event);
    });
    window.on('closed', () => {
      if (this.window === window) {
        this.window = null;
        this.loaded = false;
      }
    });

    void window.loadFile(path.join(__dirname, 'splash', 'splash.html'));
    return window;
  }

  setView(view: SplashView): void {
    const previousKind = this.view.kind;
    this.view = view;
    this.revision += 1;
    // Reuse an open window without raising it: status ticks arrive about
    // once a second during a slow start, and focusing on each one steals
    // the keyboard from whatever the user switched to. Only a change of
    // view (status -> failure/connect) brings the splash forward.
    const existing = this.window && !this.window.isDestroyed() ? this.window : null;
    const window = existing ?? this.show();
    if (existing && view.kind !== previousKind) this.raise(existing);
    const height = HEIGHTS[view.kind];
    if (window.getContentSize()[1] !== height) {
      window.setContentSize(WIDTH, height, false);
      window.center();
    }
    this.send({ type: 'view', view });
  }

  status(message: string, detail?: string): void {
    this.setView(detail === undefined ? { kind: 'status', message } : { kind: 'status', message, detail });
  }

  pushLog(line: string): void {
    this.send({ type: 'log', line });
  }

  close(): void {
    ipcMain.off(IPC.splashAction, this.handleAction);
    this.queue = [];
    const window = this.window;
    this.window = null;
    this.loaded = false;
    if (window && !window.isDestroyed()) window.destroy();
  }

  private raise(window: BrowserWindow): void {
    window.show();
    window.focus();
  }

  private send(event: SplashEvent): void {
    if (!this.loaded) {
      // Bound the queue: a long `uvx` download can emit thousands of
      // lines before the document is ready.
      this.queue.push(event);
      if (this.queue.length > 200) this.queue.splice(0, this.queue.length - 200);
      return;
    }
    this.post(event);
  }

  private post(event: SplashEvent): void {
    const window = this.window;
    if (!window || window.isDestroyed()) return;
    window.webContents.send(IPC.splashEvent, event);
  }

  private readonly handleAction = (event: IpcMainEvent, payload: unknown): void => {
    // Only our own splash gets to drive this; any other webContents that
    // learns the channel name is ignored.
    if (!this.window || this.window.isDestroyed()) return;
    if (event.sender !== this.window.webContents) return;
    const action = parseAction(payload);
    if (!action) return;
    this.options.onAction(action);
  };
}

export function parseAction(payload: unknown): SplashAction | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const record = payload as Record<string, unknown>;
  switch (record['type']) {
    case 'retry':
      return { type: 'retry' };
    case 'quit':
      return { type: 'quit' };
    case 'spawn':
      return { type: 'spawn' };
    case 'connect': {
      const url = record['url'];
      if (typeof url !== 'string') return null;
      const token = record['token'];
      const serverId = record['serverId'];
      return {
        type: 'connect',
        url,
        ...(typeof token === 'string' && token ? { token } : {}),
        ...(typeof serverId === 'string' && serverId ? { serverId } : {}),
      };
    }
    default:
      return null;
  }
}
