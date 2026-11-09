import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { RunLog, MemoryLog } from '../src/main/log.js';
import { INSTALL_HANDOFF_TIMEOUT_MS, runQuitSequence, type QuitSequenceDeps } from '../src/main/quit.js';

/**
 * The quit interlock.
 *
 * One property is worth the whole file: the update install starts strictly
 * after the server teardown has resolved. In a real quit that ordering is
 * invisible — the app is gone either way, and the failure it prevents (an
 * installer rewriting the app bundle while a uvicorn and its Docker
 * sandboxes are still coming down out of it) shows up later, as an orphan
 * nobody can trace back here.
 */

interface Rig {
  deps: QuitSequenceDeps;
  /** Every effect, in the order it happened. */
  order: string[];
  exits: number[];
  log: MemoryLog;
}

function rig(
  overrides: {
    stopServer?: () => Promise<void>;
    installIfPending?: () => boolean;
    keepServer?: boolean;
    installPending?: boolean;
  } = {},
): Rig {
  const order: string[] = [];
  const exits: number[] = [];
  const log = new MemoryLog();

  const deps: QuitSequenceDeps = {
    stopServer:
      overrides.stopServer ??
      (() => {
        order.push('stopServer');
        return Promise.resolve();
      }),
    releaseServer: () => {
      order.push('releaseServer');
    },
    keepServer: () => overrides.keepServer ?? false,
    installPending: () => overrides.installPending ?? false,
    closeLog: () => order.push('closeLog'),
    installIfPending:
      overrides.installIfPending ??
      (() => {
        order.push('installIfPending');
        return false;
      }),
    exit: (code) => {
      order.push(`exit(${code})`);
      exits.push(code);
    },
    setTimeoutImpl: (fn, ms) => {
      order.push(`arm(${ms})`);
      setTimeout(fn, ms);
    },
    log,
  };

  return { deps, order, exits, log };
}

beforeEach(() => {
  vi.useFakeTimers({ now: 1_700_000_000_000 });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('runQuitSequence', () => {
  it('does not touch the installer until the server has fully stopped', async () => {
    const order: string[] = [];
    let releaseStop = (): void => {};
    const stopped = new Promise<void>((resolve) => {
      releaseStop = () => {
        order.push('stopServer resolved');
        resolve();
      };
    });

    const test = rig({
      stopServer: () => {
        order.push('stopServer called');
        return stopped;
      },
      installIfPending: () => {
        order.push('installIfPending');
        return false;
      },
    });
    // One order array for both the local and the rig-supplied effects.
    const deps: QuitSequenceDeps = {
      ...test.deps,
      closeLog: () => order.push('closeLog'),
      exit: (code) => order.push(`exit(${code})`),
    };

    const sequence = runQuitSequence(deps);
    await vi.advanceTimersByTimeAsync(0);
    expect(order).toEqual(['stopServer called']);

    releaseStop();
    await sequence;

    expect(order).toEqual([
      'stopServer called',
      'stopServer resolved',
      'installIfPending',
      'closeLog',
      'exit(0)',
    ]);
  });

  it('closes the log after the installer handoff __F0997_F1016', async () => {
    const test: Rig = rig({
      installIfPending: () => {
        test.order.push('installIfPending');
        return true;
      },
    });
    await runQuitSequence(test.deps);

    expect(test.order.indexOf('installIfPending')).toBeLessThan(test.order.indexOf('closeLog'));
  });

  it('exits immediately when there is nothing to install', async () => {
    const test = rig();
    await runQuitSequence(test.deps);

    expect(test.exits).toEqual([0]);
    expect(test.order.some((entry) => entry.startsWith('arm('))).toBe(false);
  });

  it('lets the installer drive the quit, with a backstop', async () => {
    const test = rig({ installIfPending: () => true });

    await runQuitSequence(test.deps);

    // quitAndInstall issues its own app.quit(); exiting here would kill
    // the process mid-handoff.
    expect(test.exits).toEqual([]);
    expect(test.order).toContain(`arm(${INSTALL_HANDOFF_TIMEOUT_MS})`);

    await vi.advanceTimersByTimeAsync(INSTALL_HANDOFF_TIMEOUT_MS);

    // A handoff that never took the process down must not leave a
    // windowless process holding the single-instance lock.
    expect(test.exits).toEqual([0]);
  });

  it('still quits when the server teardown fails', async () => {
    const test = rig({
      stopServer: () => Promise.reject(new Error('server would not stop')),
    });

    await runQuitSequence(test.deps);

    expect(test.order).toEqual(['installIfPending', 'closeLog', 'exit(0)']);
    expect(test.log.lines.join('\n')).toContain('server would not stop');
  });

  it('installs even when the teardown failed — the update is still wanted', async () => {
    const test = rig({
      stopServer: () => Promise.reject(new Error('timeout')),
      installIfPending: () => {
        test.order.push('installIfPending');
        return true;
      },
    });

    await runQuitSequence(test.deps);

    expect(test.order).toContain('installIfPending');
    expect(test.exits).toEqual([]);
  });
});

describe('keep-alive at quit (MS-04)', () => {
  it('releases the local server instead of stopping it when keepServer() says so (MS-04)', async () => {
    const test = rig({ keepServer: true });

    await runQuitSequence(test.deps);

    expect(test.order).toEqual(['releaseServer', 'installIfPending', 'closeLog', 'exit(0)']);
    expect(test.order).not.toContain('stopServer');
  });

  it('stops the server as before when keep-alive is off (MS-04)', async () => {
    const test = rig({ keepServer: false });

    await runQuitSequence(test.deps);

    expect(test.order).toEqual(['stopServer', 'installIfPending', 'closeLog', 'exit(0)']);
  });

  it('a pending install always stops the server first, keep-alive or not (MS-04)', async () => {
    const test: Rig = rig({
      keepServer: true,
      installPending: true,
      installIfPending: () => {
        test.order.push('installIfPending');
        return true;
      },
    });

    await runQuitSequence(test.deps);

    // stop → install → close: the installer never runs beside a live server.
    expect(test.order).toEqual(['stopServer', 'installIfPending', 'closeLog', `arm(${INSTALL_HANDOFF_TIMEOUT_MS})`]);
    expect(test.order).not.toContain('releaseServer');
  });

  it('still quits when the release throws (MS-04)', async () => {
    const test = rig({ keepServer: true });
    const deps: QuitSequenceDeps = {
      ...test.deps,
      releaseServer: () => {
        throw new Error('release failed');
      },
    };

    await runQuitSequence(deps);

    expect(test.exits).toEqual([0]);
    expect(test.log.lines.join('\n')).toContain('release failed');
  });
});

 it('keeps installer handoff diagnostics in the real file before closing __F0997_F1016', async () => {
  vi.useRealTimers();
  const dir = await mkdtemp(path.join(os.tmpdir(), 'desktop-quit-'));
  const file = path.join(dir, 'server.log');
  const log = new RunLog(file);
  try {
    const test = rig();
    await runQuitSequence({ ...test.deps, log, closeLog: () => log.close(),
      installIfPending: () => { log.shell('quitAndInstall failed: fixture handoff refused'); return false; } });
    await expect.poll(async () => readFile(file, 'utf8')).toContain('fixture handoff refused');
  } finally { log.close(); await rm(dir, { recursive: true, force: true }); }
 });
