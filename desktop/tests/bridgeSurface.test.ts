import { describe, expect, it } from 'vitest';
import {
  BRIDGE_PROTOCOL,
  DEFAULT_INTERFACE_SCALE,
  INTERFACE_SCALES,
  IPC,
  isInterfaceScale,
  stepInterfaceScale,
  LOCAL_SERVER_ID,
  type CollimateDesktop,
  type GuestOps,
  type ServerInfo,
  type ServersOps,
} from '../src/shared/bridge.js';

/**
 * The shell's guest surface, frozen.
 *
 * Four of these verbs carry the SDK-facing RPC: a generator's capture
 * context reaches a page through them
 * (content/generators/_web_shared/protocol.py `_RPC_METHODS`), and the webview
 * renderer's `answerRpc` is the switch on the other end — a contract test
 * in the SDK (sdk/tests/test_web_rpc_contract.py) freezes those two, and this
 * one freezes the shell leg. The RPC's fifth verb, `html`, never reaches
 * the shell: the renderer answers it itself via `evaluate`, so the shell
 * side is exactly these four channels. A failure here means "you are
 * changing the shell<->page protocol" — do it deliberately or not at all.
 *
 * Channel STRINGS are frozen too, not just the type: a packaged shell and
 * the page it hosts can be different builds, so renaming a channel is a
 * silent runtime break, never a type error. The remaining ops are the
 * page's own chrome (back/forward/reload/devtools/partition) and guest
 * lifecycle (create/destroy/visibility/z-order/layout) and never appear
 * on the SDK wire.
 */

// The SDK-facing verbs, spelled as GuestOps method names.
const SDK_RPC_OPS = ['evaluate', 'capturePng', 'currentUrl', 'navigate'] as const;
// Page-only chrome — not on the SDK wire.
const CHROME_OPS = ['goBack', 'goForward', 'reload', 'setGuestDevice', 'openDevTools', 'clearPartition'] as const;
// Guest lifecycle — new in bridge v2, page-only, not on the SDK wire.
const LIFECYCLE_OPS = ['createGuest', 'destroyGuest', 'setGuestVisible', 'raiseGuest', 'layoutGuests'] as const;

// Compile-time leg of the freeze: every name is a real GuestOps method,
// so a rename over in bridge.ts fails the build before it fails the test.
const _sdkKeys: readonly (keyof GuestOps)[] = SDK_RPC_OPS;
const _chromeKeys: readonly (keyof GuestOps)[] = CHROME_OPS;
const _lifecycleKeys: readonly (keyof GuestOps)[] = LIFECYCLE_OPS;
void _sdkKeys;
void _chromeKeys;
void _lifecycleKeys;

describe('guest surface freeze', () => {
  it('the SDK-facing ops are exactly four, disjoint from everything else', () => {
    expect(SDK_RPC_OPS).toHaveLength(4);
    expect(CHROME_OPS).toHaveLength(6);
    expect(LIFECYCLE_OPS).toHaveLength(5);
    expect(new Set([...SDK_RPC_OPS, ...CHROME_OPS, ...LIFECYCLE_OPS]).size).toBe(15);
  });

  it('each SDK-facing op has one frozen IPC channel', () => {
    expect({
      evaluate: IPC.guestEvaluate,
      capturePng: IPC.guestCapturePng,
      currentUrl: IPC.guestCurrentUrl,
      navigate: IPC.guestNavigate,
    }).toEqual({
      evaluate: 'collimate:guest-evaluate',
      capturePng: 'collimate:guest-capture-png',
      currentUrl: 'collimate:guest-current-url',
      navigate: 'collimate:guest-navigate',
    });
  });

  it('the lifecycle ops have their own frozen channels', () => {
    expect({
      createGuest: IPC.guestCreate,
      destroyGuest: IPC.guestDestroy,
      setGuestVisible: IPC.guestSetVisible,
      raiseGuest: IPC.guestRaise,
      layoutGuests: IPC.guestLayout,
    }).toEqual({
      createGuest: 'collimate:guest-create',
      destroyGuest: 'collimate:guest-destroy',
      setGuestVisible: 'collimate:guest-set-visible',
      raiseGuest: 'collimate:guest-raise',
      layoutGuests: 'collimate:guest-layout',
    });
  });

  it('the whole channel map is frozen — a new channel is a deliberate edit', () => {
    expect(Object.keys(IPC).sort()).toEqual([
      'clearPartition',
      'guestCapturePlaceholder',
      'guestCapturePng',
      'guestCreate',
      'guestCurrentUrl',
      'guestDestroy',
      'guestEvaluate',
      'guestEvent',
      'guestGoBack',
      'guestGoForward',
      'guestLayout',
      'guestNavigate',
      'guestOpenDevTools',
      'guestRaise',
      'guestReload',
      'guestSetDevice',
      'guestSetVisible',
      'openExternal',
      'pickFolder',
      'splashAction',
      'splashEvent',
      'titleBarClose',
      'titleBarIsMaximized',
      'titleBarMenus',
      'titleBarMinimize',
      'titleBarPopupMenu',
      'titleBarToggleMaximize',
      'interfaceScale',
      'windowTheme',
      'serversList',
      'serversAdd',
      'serversUpdate',
      'serversRemove',
      'serversProbe',
      'serversOpen',
      'serversOpenChannel',
      'serversStartLocal',
      'serversStopLocal',
      'shellPrefsGet',
      'shellPrefsSet',
      'serversChanged',
      'shellCommand',
    ].sort());
    const channels = Object.values(IPC);
    expect(new Set(channels).size).toBe(channels.length);
    expect(channels.every((channel) => channel.startsWith('collimate:'))).toBe(true);
  });
});

describe('guest device (GN-04)', () => {
  it('has its own frozen channel (GN-04)', () => {
    expect(IPC.guestSetDevice).toBe('collimate:guest-set-device');
  });
});

describe('servers registry (MS-02)', () => {
  // Compile-time leg: the members are part of the page-facing bridge.
  const _members: readonly (keyof CollimateDesktop)[] = ['servers', 'onServersChanged', 'onShellCommand'];
  const _ops: readonly (keyof ServersOps)[] = [
    'list', 'add', 'update', 'remove', 'probe', 'openServer', 'openChannel',
    'startLocal', 'stopLocal', 'getPrefs', 'setPrefs',
  ];
  void _members;
  void _ops;

  it('is protocol 6 (MS-02)', () => {
    expect(BRIDGE_PROTOCOL).toBe(6);
  });

  it('has its frozen channel names (MS-02)', () => {
    expect({
      serversList: IPC.serversList,
      serversAdd: IPC.serversAdd,
      serversUpdate: IPC.serversUpdate,
      serversRemove: IPC.serversRemove,
      serversProbe: IPC.serversProbe,
      serversOpen: IPC.serversOpen,
      serversOpenChannel: IPC.serversOpenChannel,
      serversStartLocal: IPC.serversStartLocal,
      serversStopLocal: IPC.serversStopLocal,
      shellPrefsGet: IPC.shellPrefsGet,
      shellPrefsSet: IPC.shellPrefsSet,
      serversChanged: IPC.serversChanged,
      shellCommand: IPC.shellCommand,
    }).toEqual({
      serversList: 'collimate:servers-list',
      serversAdd: 'collimate:servers-add',
      serversUpdate: 'collimate:servers-update',
      serversRemove: 'collimate:servers-remove',
      serversProbe: 'collimate:servers-probe',
      serversOpen: 'collimate:servers-open',
      serversOpenChannel: 'collimate:servers-open-channel',
      serversStartLocal: 'collimate:servers-start-local',
      serversStopLocal: 'collimate:servers-stop-local',
      shellPrefsGet: 'collimate:shell-prefs-get',
      shellPrefsSet: 'collimate:shell-prefs-set',
      serversChanged: 'collimate:servers-changed',
      shellCommand: 'collimate:shell-command',
    });
  });

  it('names the window server by id and kind, never by a mode (MS-02)', () => {
    const info: ServerInfo = {
      id: LOCAL_SERVER_ID,
      kind: 'local',
      name: 'Local',
      url: 'http://127.0.0.1:5001',
      port: 5001,
      dataDir: '/data',
      serverVersion: '0.1.0',
    };
    expect(Object.keys(info).sort()).toEqual(['dataDir', 'id', 'kind', 'name', 'port', 'serverVersion', 'url']);
    expect(LOCAL_SERVER_ID).toBe('local');
  });
});

describe('interface scale (RS-05)', () => {
  // Compile-time leg: the members are part of the page-facing bridge.
  const _members: readonly (keyof CollimateDesktop)[] = ['getInterfaceScale', 'setInterfaceScale'];
  void _members;

  it('has its own frozen channel (RS-05)', () => {
    expect(IPC.interfaceScale).toBe('collimate:interface-scale');
  });

  it('offers sizes around the 100% default (RS-05)', () => {
    expect(INTERFACE_SCALES).toEqual([0.7, 0.8, 0.9, 1, 1.1, 1.25, 1.5]);
    expect(DEFAULT_INTERFACE_SCALE).toBe(1);
    for (const scale of INTERFACE_SCALES) expect(isInterfaceScale(scale)).toBe(true);
    for (const junk of [0.75, 1.2, 1.75, '1', null, undefined, Number.NaN]) expect(isInterfaceScale(junk)).toBe(false);
  });

  it('steps through the list, stopping at the ends, and resets to the default (RS-05)', () => {
    expect(stepInterfaceScale(1, 'in')).toBe(1.1);
    expect(stepInterfaceScale(1.1, 'in')).toBe(1.25);
    expect(stepInterfaceScale(1.25, 'in')).toBe(1.5);
    expect(stepInterfaceScale(1.5, 'in')).toBe(1.5);
    expect(stepInterfaceScale(1, 'out')).toBe(0.9);
    expect(stepInterfaceScale(0.9, 'out')).toBe(0.8);
    expect(stepInterfaceScale(0.7, 'out')).toBe(0.7);
    expect(stepInterfaceScale(1.25, 'reset')).toBe(1);
    // Off the list counts as the default.
    expect(stepInterfaceScale(3, 'in')).toBe(1.1);
  });
});

describe('folder picker', () => {
  // Compile-time leg: the member is part of the page-facing bridge.
  const _members: readonly (keyof CollimateDesktop)[] = ['pickFolder'];
  void _members;

  it('has its own frozen channel', () => {
    expect(IPC.pickFolder).toBe('collimate:pick-folder');
  });
});
