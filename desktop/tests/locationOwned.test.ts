import { describe, expect, it } from 'vitest';
import { channelArgs, locationOwnedDialog, parseStartupRefusal } from '../src/main/locationOwned.js';

/**
 * The location-owned refusal's pure parts (plan 1105 phase 9): the line the
 * server writes, the one-spawn flags, and the dialog's wording. The
 * controller and the respawn are in server.test.ts.
 */

const LINE = { collimate_startup_refused: 'location_owned', path: '/c', owner_data_dir: '/o', channels: 1 };

describe('parseStartupRefusal', () => {
  it('reads the documented line, padded or not', () => {
    expect(parseStartupRefusal(JSON.stringify(LINE))).toEqual({ path: '/c', ownerDataDir: '/o', channels: 1 });
    expect(parseStartupRefusal(`  ${JSON.stringify(LINE)}\r`)).toEqual({ path: '/c', ownerDataDir: '/o', channels: 1 });
  });

  it('ignores ordinary output, other refusals and malformed fields', () => {
    for (const line of [
      '',
      'INFO: Uvicorn running',
      `INFO: ${JSON.stringify(LINE)}`,
      '{"collimate_startup_refused": "location_owned", "path": "/c"',
      JSON.stringify({ ...LINE, collimate_startup_refused: 'other' }),
      JSON.stringify({ ...LINE, path: 3 }),
      JSON.stringify({ ...LINE, owner_data_dir: null }),
      JSON.stringify({ ...LINE, channels: -1 }),
      JSON.stringify({ ...LINE, channels: 1.5 }),
      JSON.stringify({ ...LINE, channels: '3' }),
    ]) {
      expect(parseStartupRefusal(line)).toBeNull();
    }
  });
});

describe('channelArgs', () => {
  it('passes the profile location, a claim beside it, or a chosen folder in its place', () => {
    expect(channelArgs(undefined)).toEqual([]);
    expect(channelArgs('/dev-channels')).toEqual(['--channels-dir', '/dev-channels']);
    expect(channelArgs(undefined, { claimChannels: true })).toEqual(['--claim-channels']);
    expect(channelArgs('/dev-channels', { claimChannels: true })).toEqual(['--channels-dir', '/dev-channels', '--claim-channels']);
    expect(channelArgs(undefined, { channelsDir: '/srv' })).toEqual(['--channels-dir', '/srv']);
    expect(channelArgs('/dev-channels', { channelsDir: '/srv' })).toEqual(['--channels-dir', '/srv']);
  });
});

describe('locationOwnedDialog', () => {
  it('counts one channel in the singular', () => {
    expect(locationOwnedDialog({ path: '/c', ownerDataDir: '/o', channels: 1 }).message).toBe(
      'The channel location /c belongs to the Collimate installation at /o (1 channel).',
    );
    expect(locationOwnedDialog({ path: '/c', ownerDataDir: '/o', channels: 0 }).message).toContain('(0 channels)');
  });
});
