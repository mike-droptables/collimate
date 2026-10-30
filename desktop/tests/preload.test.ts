import { afterEach, describe, expect, it, vi } from 'vitest';
import { ARG_BOOTSTRAP, ARG_SURFACE, BRIDGE_PROTOCOL, IPC, type CollimateDesktop, type RendererBootstrap } from '../src/shared/bridge.js';

/**
 * The preload's main-surface bridge, exposed against a fake Electron: what
 * lands on `window.collimateDesktop` and which IPC each op sends.
 */

const electron = vi.hoisted(() => ({
  exposed: new Map<string, unknown>(),
  sent: [] as unknown[][],
  invoked: [] as unknown[][],
  /** What the next invoke answers with. */
  reply: undefined as unknown,
  listeners: new Map<string, (event: unknown, payload: unknown) => void>(),
}));

vi.mock('electron', () => ({
  contextBridge: {
    exposeInMainWorld: (key: string, api: unknown) => electron.exposed.set(key, api),
  },
  ipcRenderer: {
    send: (...args: unknown[]) => electron.sent.push(args),
    invoke: async (...args: unknown[]) => { electron.invoked.push(args); return electron.reply; },
    on: (channel: string, listener: (event: unknown, payload: unknown) => void) => {
      electron.listeners.set(channel, listener);
    },
  },
  webFrame: { getZoomFactor: () => 1 },
}));

const bootstrap: RendererBootstrap = {
  shellVersion: '0.0.0-test',
  server: {
    id: 'remote-1',
    kind: 'remote',
    name: 'Box',
    url: 'http://127.0.0.1:5001',
    port: 5001,
    dataDir: null,
    serverVersion: null,
  },
  capabilities: {
    nativeGuests: true,
    guestCapture: true,
    persistentPartitions: true,
    directLoopback: true,
    externalOpen: true,
    customTitleBar: true,
    customWindowControls: true,
  },
  interfaceScale: 1.1,
};

const argv = process.argv;

async function loadMainSurface(): Promise<CollimateDesktop> {
  vi.resetModules();
  electron.exposed.clear();
  electron.sent.length = 0;
  electron.invoked.length = 0;
  electron.reply = undefined;
  electron.listeners.clear();
  process.argv = [
    ...argv,
    `${ARG_SURFACE}main`,
    `${ARG_BOOTSTRAP}${Buffer.from(JSON.stringify(bootstrap), 'utf8').toString('base64')}`,
  ];
  await import('../src/preload/index.js');
  return electron.exposed.get('collimateDesktop') as CollimateDesktop;
}

afterEach(() => {
  process.argv = argv;
});

describe('preload main surface', () => {
  it('exposes setWindowTheme, sending the theme on its own channel (S05)', async () => {
    const desktop = await loadMainSurface();
    expect(typeof desktop.setWindowTheme).toBe('function');

    const theme = { background: '#151311', overlay: '#1c1a17', symbol: '#e2ddd4' };
    desktop.setWindowTheme(theme);
    expect(electron.sent).toEqual([[IPC.windowTheme, theme]]);
  });

  it('reads the interface scale from the bootstrap and sends a valid one on its own channel (RS-05)', async () => {
    const desktop = await loadMainSurface();
    expect(desktop.getInterfaceScale()).toBe(1.1);

    desktop.setInterfaceScale(1.25);
    expect(desktop.getInterfaceScale()).toBe(1.25);
    expect(electron.sent).toEqual([[IPC.interfaceScale, 1.25]]);

    // Off-list factors never leave the page.
    for (const bad of [1.75, 0, Number.NaN, '1' as unknown as number]) desktop.setInterfaceScale(bad);
    expect(electron.sent).toEqual([[IPC.interfaceScale, 1.25]]);
    expect(desktop.getInterfaceScale()).toBe(1.25);
  });

  it('follows the scale main reports it applied, ignoring an off-list one (RS-05)', async () => {
    const desktop = await loadMainSurface();
    const report = electron.listeners.get(IPC.interfaceScale);
    expect(report).toBeTypeOf('function');
    report!({}, 0.9);
    expect(desktop.getInterfaceScale()).toBe(0.9);
    report!({}, 2);
    expect(desktop.getInterfaceScale()).toBe(0.9);
  });

  it('opens the native folder dialog on its own channel, answering a path or null', async () => {
    const desktop = await loadMainSurface();
    electron.reply = '/home/me/sdk';
    expect(await desktop.pickFolder()).toBe('/home/me/sdk');
    electron.reply = null;
    expect(await desktop.pickFolder()).toBeNull();
    electron.reply = { not: 'a path' };
    expect(await desktop.pickFolder()).toBeNull();
    expect(electron.invoked).toEqual([['collimate:pick-folder'], ['collimate:pick-folder'], ['collimate:pick-folder']]);
  });

  it('exposes setGuestDevice on its own channel (GN-04)', async () => {
    const desktop = await loadMainSurface();
    await desktop.guest.setGuestDevice('g1', 'phone');
    expect(electron.invoked).toEqual([['collimate:guest-set-device', { guestId: 'g1', device: 'phone' }]]);
  });
});

describe('preload servers registry (MS-02)', () => {
  it('reports protocol 6 and the window server by id, kind and name (MS-02)', async () => {
    const desktop = await loadMainSurface();
    expect(desktop.bridge).toBe(BRIDGE_PROTOCOL);
    expect(desktop.bridge).toBe(6);
    expect(desktop.server).toEqual(bootstrap.server);
    expect(desktop.server).not.toHaveProperty('mode');
  });

  it('sends every op on its frozen channel with an object payload (MS-02)', async () => {
    const desktop = await loadMainSurface();
    electron.reply = { value: null };
    await desktop.servers.remove('a');
    await desktop.servers.openServer('a');
    await desktop.servers.openChannel('a', 'c1');
    await desktop.servers.startLocal();
    await desktop.servers.stopLocal();
    electron.reply = { value: { keepLocalRunning: true } };
    await desktop.servers.getPrefs();
    await desktop.servers.setPrefs({ keepLocalRunning: true });
    electron.reply = { value: { ok: false, message: 'nope' } };
    await desktop.servers.probe({ url: 'http://box:5001', token: 't' });
    electron.reply = { value: { id: 'a' } };
    await desktop.servers.add({ url: 'http://box:5001', name: 'Box', token: 't' });
    await desktop.servers.update({ id: 'a', name: 'Lab' });
    electron.reply = { value: [] };
    await desktop.servers.list();
    expect(electron.invoked).toEqual([
      ['collimate:servers-remove', { id: 'a' }],
      ['collimate:servers-open', { id: 'a' }],
      ['collimate:servers-open-channel', { id: 'a', channelId: 'c1' }],
      ['collimate:servers-start-local', undefined],
      ['collimate:servers-stop-local', undefined],
      ['collimate:shell-prefs-get', undefined],
      ['collimate:shell-prefs-set', { keepLocalRunning: true }],
      ['collimate:servers-probe', { url: 'http://box:5001', token: 't' }],
      ['collimate:servers-add', { url: 'http://box:5001', name: 'Box', token: 't' }],
      ['collimate:servers-update', { id: 'a', name: 'Lab' }],
      ['collimate:servers-list', undefined],
    ]);
  });

  it("rejects with the shell's own sentence (MS-02)", async () => {
    const desktop = await loadMainSurface();
    electron.reply = { error: 'http://box:5001 requires an access token.' };
    await expect(desktop.servers.add({ url: 'http://box:5001' })).rejects.toThrow(
      'http://box:5001 requires an access token.',
    );
    electron.reply = undefined;
    await expect(desktop.servers.list()).rejects.toThrow('the shell gave no answer');
  });

  it('fans servers-changed and shell-command out to subscribers until they unsubscribe (MS-02)', async () => {
    const desktop = await loadMainSurface();
    const seen: unknown[] = [];
    const commands: unknown[] = [];
    const off = desktop.onServersChanged((entries) => seen.push(entries));
    desktop.onServersChanged(() => {
      throw new Error('a bad subscriber');
    });
    const offCommands = desktop.onShellCommand((command) => commands.push(command));
    const changed = electron.listeners.get('collimate:servers-changed');
    const command = electron.listeners.get('collimate:shell-command');
    expect(changed).toBeTypeOf('function');
    expect(command).toBeTypeOf('function');

    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    changed!({}, [{ id: 'local' }]);
    changed!({}, 'junk');
    command!({}, { type: 'openServers' });
    command!({}, { type: 'localStopFailed', message: 'The local server could not stop.' });
    command!({}, { type: 'localStopFailed', message: 3 });
    command!({}, { type: 'formatDisk' });
    off();
    offCommands();
    changed!({}, [{ id: 'later' }]);
    command!({}, { type: 'openServers' });
    errors.mockRestore();

    expect(seen).toEqual([[{ id: 'local' }]]);
    expect(commands).toEqual([
      { type: 'openServers' },
      { type: 'localStopFailed', message: 'The local server could not stop.' },
    ]);
  });
});
