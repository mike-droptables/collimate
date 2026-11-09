import type { LogSink } from './log.js';
import { errorMessage } from './errors.js';

/**
 * The quit sequence, and the interlock between shutting the server down
 * and installing an update.
 *
 * The order is the whole content of this file, and it is not negotiable:
 *
 *   1. stop or release the local server
 *                           — stop: POST /api/system/shutdown, then
 *                             signals; this is what stops the generator
 *                             subprocess tree and its Docker sandboxes.
 *                             release: leave it running with its
 *                             server.json for the next launch to adopt
 *                             ("Quit, Keep Server Running", or the
 *                             keepLocalRunning setting)
 *   2. attempt installation — retain the handoff/failure diagnostics
 *   3. close the log, exit  — the installer owns a successful handoff
 *
 * A pending install always stops the server, whatever keep-alive says:
 * an installer must never rewrite the app directory while a uvicorn the
 * app spawned is still running out of it, with containers still up — and
 * the relaunched version would not adopt it anyway (server.json records
 * the shell version). Swap 1 and 3 and that is exactly what happens; the
 * new process stops the stale server on its next start (server.json /
 * resolveRecord), but only after the user has watched it fail to bind.
 *
 * Pure and dependency-injected for the same reason ServerController is:
 * "did the install really happen strictly after the stop resolved" is a
 * question about ordering, and ordering is exactly what a real Electron
 * quit will not let you observe.
 *
 * Two Electron traps this shape exists to avoid:
 *
 * - **The Squirrel.Mac / before-quit trap.** `quitAndInstall` calls
 *   `app.quit()` itself. If `before-quit` fires again and something
 *   calls `event.preventDefault()`, the quit is cancelled — and because
 *   quitAndInstall has already handed the update off to the ShipIt
 *   helper, the app just… stays open, having silently declined to
 *   install. The `quitting` flag in index.ts is what lets that second
 *   `app.quit()` through untouched.
 * - **The single-instance lock.** The installer relaunches the app. The
 *   handoff therefore happens at the very end of the sequence, once this
 *   process has nothing left to do, so the relaunched instance isn't
 *   racing the old one for `requestSingleInstanceLock()` — the loser of
 *   that race quits immediately and the update looks like it did nothing.
 */

/**
 * How long quitAndInstall gets to take the process down before we do.
 *
 * quitAndInstall drives its own `app.quit()`, so on the happy path this
 * timer never fires. It exists for the unhappy one: a handoff that fails
 * (a Squirrel helper that can't launch, an NSIS elevation the user
 * cancels) would otherwise leave a windowless process alive, holding the
 * single-instance lock, with no way to reach it but Task Manager.
 */
export const INSTALL_HANDOFF_TIMEOUT_MS = 15_000;

export interface QuitSequenceDeps {
  /** Graceful server teardown. Owns its own splash/status reporting. */
  stopServer: () => Promise<void>;
  /** Leave the local server running for the next launch (ServerController.release). */
  releaseServer: () => void;
  /** True when this quit should keep the local server running. */
  keepServer: () => boolean;
  /** True when an update is staged to install at this quit. */
  installPending: () => boolean;
  closeLog: () => void;
  /** True when quitAndInstall ran and now owns the rest of the shutdown. */
  installIfPending: () => boolean;
  exit: (code: number) => void;
  setTimeoutImpl: (fn: () => void, ms: number) => void;
  log: LogSink;
}

export async function runQuitSequence(deps: QuitSequenceDeps): Promise<void> {
  try {
    if (deps.keepServer() && !deps.installPending()) {
      deps.releaseServer();
    } else {
      await deps.stopServer();
    }
  } catch (error) {
    // A teardown that failed must not strand the app on screen: the user
    // asked to quit, and refusing to because the server misbehaved is the
    // one outcome worse than a leaked process.
    deps.log.shell(`shutdown failed: ${errorMessage(error)}`);
  }

  const installing = deps.installIfPending();
  deps.closeLog();

  if (installing) {
    deps.setTimeoutImpl(() => deps.exit(0), INSTALL_HANDOFF_TIMEOUT_MS);
    // Deliberately no exit() here: calling it now would kill the process
    // mid-handoff, before the installer has been fully spawned.
    return;
  }

  deps.exit(0);
}
