import { afterEach, describe, expect, it, vi } from 'vitest';
import { ARG_BOOTSTRAP, ARG_SURFACE, BRIDGE_PROTOCOL, IPC, type BuildInfo, type CollimateDesktop, type RendererBootstrap } from '../src/shared/bridge.js';

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
  editingPrefs: { spellcheck: false, statusOnRun: true },
  buildInfo: { version: '0.0.0-test', commit: null, dirty: null, builtAt: null, serverEnvironmentHash: null },
  profileName: 'Collimate Dev',
};

const argv = process.argv;

async function loadMainSurface(over: Partial<RendererBootstrap> = {}): Promise<CollimateDesktop> {
  vi.resetModules();
  electron.exposed.clear();
  electron.sent.length = 0;
  electron.invoked.length = 0;
  electron.reply = undefined;
  electron.listeners.clear();
  process.argv = [
    ...argv,
    `${ARG_SURFACE}main`,
    `${ARG_BOOTSTRAP}${Buffer.from(JSON.stringify({ ...bootstrap, ...over }), 'utf8').toString('base64')}`,
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

  it('answers buildInfo() with the five fields main put in the bootstrap, and nothing else', async () => {
    const info = {
      version: '0.4.1', commit: 'c'.repeat(40), dirty: true, builtAt: '2026-10-31T22:00:00Z', serverEnvironmentHash: null,
    };
    // Anything else the main process put there stays main-side.
    const stamped = await loadMainSurface({ buildInfo: { ...info, wheel: {} } as BuildInfo });
    expect(stamped.buildInfo()).toEqual(info);
    expect(stamped).not.toHaveProperty('localBuild');

    const dev = await loadMainSurface();
    expect(dev.buildInfo()).toEqual(bootstrap.buildInfo);
  });

  it('answers getProfileName() synchronously with the profile main put in the bootstrap', async () => {
    expect((await loadMainSurface()).getProfileName()).toBe('Collimate Dev');
    expect((await loadMainSurface({ profileName: 'Collimate' })).getProfileName()).toBe('Collimate');
  });

  it('sends the updater calls on their own channels and unwraps the answers', async () => {
    const desktop = await loadMainSurface();
    electron.reply = { value: { status: 'unavailable', reason: 'no update feed' } };
    expect(await desktop.checkForUpdates()).toEqual({ status: 'unavailable', reason: 'no update feed' });
    electron.reply = { value: false };
    expect(await desktop.getAutomaticUpdates()).toBe(false);
    expect(await desktop.setAutomaticUpdates(false)).toBe(false);
    electron.reply = { error: 'collimate-servers/not-app-window' };
    await expect(desktop.setAutomaticUpdates(true)).rejects.toThrow('not-app-window');
    expect(electron.invoked).toEqual([
      ['collimate:updates-check', undefined],
      ['collimate:automatic-updates-get', undefined],
      ['collimate:automatic-updates-set', false],
      ['collimate:automatic-updates-set', true],
    ]);
  });

  it('answers getEditingPrefs synchronously from the bootstrap, then from every push, and tells subscribers', async () => {
    const desktop = await loadMainSurface();
    expect(desktop.getEditingPrefs()).toEqual({ spellcheck: false, statusOnRun: true });

    const heard: unknown[] = [];
    const off = desktop.onEditingPrefsChanged((prefs) => heard.push(prefs));
    const push = electron.listeners.get(IPC.editingPrefs);
    expect(push).toBeTypeOf('function');
    push!({}, { spellcheck: true, statusOnRun: false });
    expect(desktop.getEditingPrefs()).toEqual({ spellcheck: true, statusOnRun: false });
    expect(heard).toEqual([{ spellcheck: true, statusOnRun: false }]);

    // Only an explicit false is off, as settings.json is read.
    push!({}, { spellcheck: 'no', statusOnRun: null });
    expect(desktop.getEditingPrefs()).toEqual({ spellcheck: true, statusOnRun: true });

    off();
    push!({}, { spellcheck: false, statusOnRun: false });
    expect(heard).toHaveLength(2);
    expect(desktop.getEditingPrefs()).toEqual({ spellcheck: false, statusOnRun: false });
  });

  it('reads a bootstrap with no editing preferences as both on', async () => {
    const desktop = await loadMainSurface({ editingPrefs: undefined as never });
    expect(desktop.getEditingPrefs()).toEqual({ spellcheck: true, statusOnRun: true });
  });

  it('sends setEditingPrefs on its own channel with only the boolean fields, and adopts the answer', async () => {
    const desktop = await loadMainSurface();
    electron.reply = { value: { spellcheck: true, statusOnRun: false } };
    const saved = await desktop.setEditingPrefs({ statusOnRun: false, spellcheck: 'yes', extra: 1 } as never);
    expect(saved).toEqual({ spellcheck: true, statusOnRun: false });
    expect(desktop.getEditingPrefs()).toEqual({ spellcheck: true, statusOnRun: false });
    expect(electron.invoked).toEqual([['collimate:editing-prefs-set', { statusOnRun: false }]]);

    electron.reply = { error: 'collimate-servers/not-app-window' };
    await expect(desktop.setEditingPrefs({ spellcheck: false })).rejects.toThrow('not-app-window');
  });

  it('sends pickFolder on its own channel with the two named fields alone, and unwraps the path', async () => {
    const desktop = await loadMainSurface();
    electron.reply = { value: '/home/me/Collimate/Channels' };
    expect(await desktop.pickFolder({ title: 'Add a location' })).toBe('/home/me/Collimate/Channels');
    electron.reply = { value: null };
    const extra = { title: 'Add a location', defaultPath: '/home/me', multi: true } as Parameters<
      CollimateDesktop['pickFolder']
    >[0];
    expect(await desktop.pickFolder(extra)).toBeNull();
    electron.reply = { error: 'collimate-dialog/not-local-server' };
    await expect(desktop.pickFolder({ title: 'Add a location' })).rejects.toThrow('not-local-server');
    // Junk from the page rejects; it never throws out of the call.
    await expect(desktop.pickFolder(null as unknown as Parameters<CollimateDesktop['pickFolder']>[0])).rejects.toThrow();
    expect(electron.invoked).toEqual([
      ['collimate:dialog-pick-folder', { title: 'Add a location' }],
      ['collimate:dialog-pick-folder', { title: 'Add a location', defaultPath: '/home/me' }],
      ['collimate:dialog-pick-folder', { title: 'Add a location' }],
    ]);
  });

  it('exposes setGuestDevice on its own channel (GN-04)', async () => {
    const desktop = await loadMainSurface();
    await desktop.guest.setGuestDevice('g1', 'phone');
    expect(electron.invoked).toEqual([['collimate:guest-set-device', { guestId: 'g1', device: 'phone' }]]);
  });
});

describe('preload servers registry (MS-02)', () => {
  it('reports its protocol and the window server by id, kind and name (MS-02)', async () => {
    const desktop = await loadMainSurface();
    expect(desktop.bridge).toBe(BRIDGE_PROTOCOL);
    expect(desktop.bridge).toBe(8);
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
    await desktop.servers.update({ id: 'a', enabled: false });
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
      ['collimate:servers-update', { id: 'a', enabled: false }],
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
