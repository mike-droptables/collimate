import { spawn, type ChildProcess } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { errorMessage } from './errors.js';

/**
 * The environment the user's terminal has, for an app the user launched
 * from a menu.
 *
 * A GUI launch inherits the desktop session's environment — systemd's, or
 * launchd's on macOS — not the one the login shell builds out of .profile,
 * .bashrc, .zshrc and friends. So PATH is whatever the session manager was
 * started with, and everything a version manager puts on PATH from an rc
 * file (nvm, fnm, volta, pyenv, asdf, homebrew's shellenv) is simply not
 * there. The same app started from a terminal has all of it. That gap is
 * not cosmetic: the server we spawn inherits our environment, so a feature
 * that shells out — the chat backend installing and running the ACP
 * adapter with the machine's node and npm — works from the terminal and
 * fails from the app menu, reporting a node version the user does not
 * think they have.
 *
 * The fix is VS Code's: at startup, ask the login shell to print its
 * environment, and fold what it says into ours before anything spawns a
 * child. This is the only login-shell discovery pass: the uv probes,
 * the server child, and everything the server itself spawns all see the
 * merged result.
 *
 * Everything here is either pure or takes its one effect through an
 * injected runner, because the interesting cases — an rc file that prints
 * a banner, a value with a newline in it, a shell that hangs — are
 * miserable to reproduce with a real shell and trivial to state as data.
 */

/**
 * Variables that describe the shell we just spawned rather than the user's
 * environment, and must not be copied into a long-lived GUI process.
 *
 * `_` is the last argument of the last command the rc file ran, `PWD` and
 * `OLDPWD` are the probe's working directory (never the app's), `SHLVL`
 * would make every launch look one level deeper, and `TERM`, `COLUMNS` and
 * `LINES` describe a terminal this process does not have — a child that
 * believes it is on a tty with 80 columns formats its output for one. The
 * two ELECTRON_ variables are the dangerous pair: they are how a process
 * is told to behave as plain Node instead of Electron, and inheriting one
 * from an rc file (or from the environment of whatever launched the
 * probe) into every child we spawn would be a spectacular, very confusing
 * failure.
 */
export const SHELL_ENV_SKIP: ReadonlySet<string> = new Set([
  '_',
  'SHLVL',
  'PWD',
  'OLDPWD',
  'TERM',
  'COLUMNS',
  'LINES',
  'ELECTRON_RUN_AS_NODE',
  'ELECTRON_NO_ATTACH_CONSOLE',
]);

/**
 * The app's own settings are never taken from an rc file either.
 *
 * This feature's job is toolchain discovery for the children we spawn —
 * PATH, a version manager's NVM_DIR/FNM_DIR/VOLTA_HOME, the API keys a
 * developer exports — not late reconfiguration of the shell itself. A
 * COLLIMATE_* value read here would arrive *after* the decisions it
 * governs and change the app under the user: COLLIMATE_APP_PROFILE is
 * already spent (main/profile.ts resolves it at module load, so a merged
 * value would only make the log disagree with the running app),
 * COLLIMATE_SHELL_ENV=0 in an rc file would be an opt-out that arrives
 * after the probe it was meant to prevent, and COLLIMATE_CONNECT_URL or
 * COLLIMATE_SERVER_SPEC would silently point a launcher-started app at a
 * different server than the same app started from a terminal. Every one of
 * these is deliberately set at launch time or in settings.json, where the
 * user can see it; an rc file is not that place.
 */
export const SHELL_ENV_SKIP_PREFIX = 'COLLIMATE_';

/** Fixed set or prefix rule — the two reasons a variable is not inherited. */
function isSkipped(key: string): boolean {
  return SHELL_ENV_SKIP.has(key) || key.startsWith(SHELL_ENV_SKIP_PREFIX);
}

/**
 * PATH's separator, hard-coded: this whole module is POSIX-only (see
 * resolveShellEnv, which returns null on win32 before anything is spawned),
 * so there is no case where node:path's platform-dependent delimiter would
 * differ, and hard-coding keeps the merge testable on any host.
 */
const PATH_SEP = ':';

export interface MergedShellEnv {
  /** The environment to use: ours, with the shell's answers folded in. */
  env: Record<string, string>;
  /** Keys whose value this merge actually changes, for the startup log. */
  changed: string[];
  /** PATH entries the shell knew about and we did not. */
  pathAdded: string[];
}

/**
 * Fold a login shell's environment into the one we have.
 *
 * The shell wins on conflicts, because the whole point is that its answer
 * is the one the user's terminal would have given — with two exceptions.
 * What the skip rules above cover is never taken at all. And PATH is
 * merged rather than replaced: a packaged app can have entries of its own
 * that the login shell has never heard of (Electron's own bundled helpers,
 * a Snap or Flatpak runtime's bin directories), and replacing PATH
 * wholesale would drop them. Shell entries come first so a version
 * manager's node wins over /usr/bin/node — which is the bug this module
 * exists to fix — and ours are appended so nothing that used to resolve
 * stops resolving.
 */
export function mergeShellEnv(
  current: NodeJS.ProcessEnv,
  fromShell: Record<string, string>,
): MergedShellEnv {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(current)) {
    if (value !== undefined) env[key] = value;
  }

  const changed: string[] = [];
  for (const [key, value] of Object.entries(fromShell)) {
    if (isSkipped(key)) continue;
    if (key === 'PATH') continue; // unioned below, never replaced
    if (current[key] === value) continue;
    env[key] = value;
    changed.push(key);
  }

  const currentPath = current['PATH'];
  const shellPath = fromShell['PATH'];
  const pathAdded: string[] = [];
  if (shellPath !== undefined) {
    const merged = unionPath(shellPath, currentPath);
    const had = new Set(splitPath(currentPath));
    for (const entry of splitPath(merged)) {
      if (!had.has(entry)) pathAdded.push(entry);
    }
    if (merged !== currentPath) {
      env['PATH'] = merged;
      changed.push('PATH');
    }
  }

  return { env, changed, pathAdded };
}

/**
 * Split a PATH, dropping empty entries. An empty entry means the current
 * working directory on POSIX — a footgun in a long-lived process whose cwd
 * is not the user's — and dedupe would collapse repeats of it anyway.
 */
function splitPath(value: string | undefined): string[] {
  if (!value) return [];
  return value.split(PATH_SEP).filter(Boolean);
}

/** First argument's entries first, then the second's, deduped, order kept. */
function unionPath(first: string | undefined, second: string | undefined): string {
  const seen = new Set<string>();
  const entries: string[] = [];
  for (const entry of [...splitPath(first), ...splitPath(second)]) {
    if (seen.has(entry)) continue;
    seen.add(entry);
    entries.push(entry);
  }
  return entries.join(PATH_SEP);
}

/**
 * Pull the environment out of what the shell printed.
 *
 * Markers rather than "parse everything": an interactive login shell is
 * entitled to print whatever it likes, and plenty of rc files do — a MOTD,
 * a fortune, a version-manager warning, a "you have mail" — both before
 * our first printf and, when a shell prints on exit, after our last one.
 * Only the text between the two markers is ours.
 *
 * Records are NUL-separated (`env -0`) so a value containing a newline
 * survives: exported shell functions and key material are both multi-line
 * and both common enough to matter. The newline fallback is for a BSD or
 * busybox `env` with no -0, where `printenv` is all we get; there a
 * multi-line value's continuation lines carry no `=` and are dropped
 * rather than guessed at.
 *
 * Two DIFFERENT markers, not one twice: POSIX shells set `_` to the last
 * argument of the previous command, and dash (Debian's /bin/sh) and
 * busybox ash export it — so at the moment `env` runs, `_=<begin marker>`
 * is part of the environment being printed, and a parser that ends the
 * body at the next occurrence of a single marker stops there, silently
 * dropping every variable after `_` (PATH included, on this machine).
 * bash and zsh overwrite `_` with the executed command's path, which is
 * why one marker looked fine under them. The end marker is the one thing
 * `_` cannot carry when `env` runs.
 *
 * Returns null when the markers are missing, which is how "the shell
 * failed before it got to our script" is distinguished from "the shell
 * printed an environment".
 */
export function parseShellEnvOutput(
  stdout: string,
  beginMarker: string,
  endMarker: string,
): Record<string, string> | null {
  const start = stdout.indexOf(beginMarker);
  if (start < 0) return null;
  const bodyStart = start + beginMarker.length;
  const end = stdout.indexOf(endMarker, bodyStart);
  if (end < 0) return null;
  const body = stdout.slice(bodyStart, end);

  const records = body.includes('\0') ? body.split('\0') : body.split('\n');
  const env: Record<string, string> = {};
  for (const record of records) {
    const eq = record.indexOf('=');
    if (eq <= 0) continue;
    const key = record.slice(0, eq);
    // A key with whitespace in it is not a key: in the newline fallback it
    // is the continuation of a value we already lost the start of, and in
    // either mode it is not something a child could look up anyway.
    if (/\s/.test(key)) continue;
    env[key] = record.slice(eq + 1);
  }
  return env;
}

export interface ShellRunRequest {
  shell: string;
  args: string[];
  env: NodeJS.ProcessEnv;
  timeoutMs: number;
}

export interface ShellRunResult {
  stdout: string;
  /** Exit status; null when the shell died without one (a signal). */
  code: number | null;
  /** Set only when the runner killed the shell for taking too long. */
  timedOut?: boolean;
}

export type ShellRunner = (request: ShellRunRequest) => Promise<ShellRunResult>;

export interface ShellEnvResult extends MergedShellEnv {
  /** The shell that answered, for the log line. */
  shell: string;
}

export interface ResolveShellEnvOptions {
  platform: NodeJS.Platform;
  env: NodeJS.ProcessEnv;
  timeoutMs?: number;
  run?: ShellRunner;
  /**
   * Why nothing came back, called at most once and only when the result is
   * null. The result stays a plain `| null` — every caller's behaviour on
   * failure is "carry on with the environment we have" — but the startup
   * log is worth a reason, because "the app menu launch behaves differently
   * from the terminal one" is the exact class of bug this module exists to
   * fix, and the first question is always whether it ran.
   */
  onSkip?: (reason: string) => void;
}

/** VS Code waits 10s; 8 leaves room for a slow rc without stalling startup. */
export const DEFAULT_SHELL_ENV_TIMEOUT_MS = 8_000;

/** 4 MB: an environment is kilobytes, and the cap bounds a runaway rc file. */
const MAX_BUFFER_BYTES = 4 * 1024 * 1024;

/**
 * `-ilc` so both the login files (.profile, .zprofile) and the interactive
 * ones (.bashrc, .zshrc) run — version managers are conventionally set up
 * in the interactive half, which is why a plain `-lc` would miss the node
 * this is all for. `command` bypasses any alias or function an rc file has
 * defined over `env`, and 2>/dev/null keeps a warning from `env -0` on a
 * system that lacks it out of the way of the `printenv` fallback.
 */
function buildScript(beginMarker: string, endMarker: string): string {
  return `printf '%s' "${beginMarker}"; command env -0 2>/dev/null || command printenv; printf '%s' "${endMarker}";`;
}

/** After the deadline's SIGKILL, how long to wait for the exit before resolving anyway. */
const KILL_GRACE_MS = 250;
/** After the shell's own exit, how long to let stdout drain before resolving. */
const EXIT_DRAIN_MS = 100;

/**
 * The default runner: spawn, collect stdout, and keep the clock ourselves.
 *
 * Not execFile. Its `timeout` sends SIGTERM and then WAITS for the child to
 * exit, and an interactive dash ignores SIGTERM (bash and zsh die on it):
 * measured against a blocking rc file under /bin/sh, a 1 s timeout came
 * back after the rc file finished — 6 to 30 s later — with `killed:
 * false`. It also calls back only once stdout reaches EOF, so a background
 * process the rc file started with the pipe inherited (`emacs --daemon &`,
 * `tmux new -d`) holds the answer until that process exits. Both would
 * park bootstrap on the splash for as long as the user's setup takes,
 * which is the one thing a startup probe must never do.
 *
 * So: the deadline is a timer here; at the deadline the shell gets
 * SIGKILL and, whether or not that lands, the promise resolves shortly
 * after with whatever was collected (the caller reads the markers and
 * keeps a complete answer). A normal run ends on the shell's `exit` event
 * rather than `close` for the same reason — the pipe can outlive the shell
 * — with a brief drain for bytes still in flight. A spawn failure (no such
 * shell) rejects, so the caller can name it.
 */
const nodeShellRunner: ShellRunner = (request) =>
  new Promise<ShellRunResult>((resolve, reject) => {
    const child: ChildProcess = spawn(request.shell, request.args, {
      env: request.env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      // A new session: the interactive shell then has no controlling terminal,
      // so it cannot take a launching terminal's foreground (and keep it if
      // SIGKILLed at the deadline) or prompt on it. VS Code's resolver does too.
      detached: true,
    });
    const chunks: Buffer[] = [];
    let bytes = 0;
    let settled = false;
    let timedOut = false;
    let drain: NodeJS.Timeout | undefined;

    const finish = (code: number | null): void => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      if (drain) clearTimeout(drain);
      // Release the pipes: a background child that inherited stdout would
      // otherwise keep the read side — and this process's interest in it —
      // alive for as long as it runs.
      child.stdout?.destroy();
      child.stderr?.destroy();
      resolve({
        stdout: Buffer.concat(chunks).toString('utf8'),
        code,
        ...(timedOut ? { timedOut: true } : {}),
      });
    };

    child.stdout?.on('data', (chunk: Buffer) => {
      if (settled) return;
      if (bytes + chunk.length > MAX_BUFFER_BYTES) {
        // A runaway rc file. Stop reading; what was collected is judged by
        // the markers like any other answer.
        child.kill('SIGKILL');
        finish(null);
        return;
      }
      chunks.push(chunk);
      bytes += chunk.length;
    });
    // stderr is not ours to read, but an unread pipe fills and blocks the
    // shell, so drain it into nothing.
    child.stderr?.resume();
    child.on('error', (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      reject(error);
    });
    child.on('exit', (code) => {
      drain = setTimeout(() => finish(code), EXIT_DRAIN_MS);
    });
    child.on('close', (code) => finish(code));

    const deadline = setTimeout(() => {
      timedOut = true;
      child.kill('SIGKILL');
      setTimeout(() => finish(null), KILL_GRACE_MS);
    }, request.timeoutMs);
  });

/**
 * Ask the login shell for its environment and merge it into ours.
 *
 * Never throws and never rejects: this runs on the startup path, with the
 * user watching a splash and waiting, and an unusual shell setup must not
 * be the reason the app fails to start. Every way it can go wrong — no
 * shell to ask, a shell that answered nothing before it was killed, output
 * we cannot read — resolves to null, meaning "keep the environment we
 * already have", which is exactly today's behaviour.
 *
 * Windows is out: there is no login-shell convention to consult, and the
 * shell has no local server there (see connect.ts bootTarget), so it
 * spawns no server whose environment would matter. COLLIMATE_SHELL_ENV=0 is the
 * opt-out for anyone whose rc files make the probe slow or destructive.
 */
export async function resolveShellEnv(
  options: ResolveShellEnvOptions,
): Promise<ShellEnvResult | null> {
  const { platform, env } = options;
  const skip = (reason: string): null => {
    options.onSkip?.(reason);
    return null;
  };

  if (platform === 'win32') return skip('not applicable on Windows');
  if (env['COLLIMATE_SHELL_ENV'] === '0') return skip('COLLIMATE_SHELL_ENV=0');
  const shell = env['SHELL'];
  if (!shell) return skip('$SHELL is unset');

  const timeoutMs = options.timeoutMs ?? DEFAULT_SHELL_ENV_TIMEOUT_MS;
  const run = options.run ?? nodeShellRunner;
  // Random per call so no rc file, and nothing in the environment being
  // printed, can contain the markers by accident and truncate the answer;
  // two distinct ones because `_` carries the first (see parseShellEnvOutput).
  const marker = `collimate-env-${randomUUID()}`;
  const beginMarker = `${marker}-begin`;
  const endMarker = `${marker}-end`;

  let result: ShellRunResult;
  try {
    result = await run({
      shell, args: ['-ilc', buildScript(beginMarker, endMarker)], env, timeoutMs,
    });
  } catch (error) {
    return skip(`${shell} could not be run: ${errorMessage(error)}`);
  }

  // How the shell process ended is judged only after reading what it
  // printed, because the two answer different questions. The markers say
  // whether the shell got as far as printing its environment; the exit
  // status says how the last thing the rc files did turned out, and on a
  // real machine that is routinely unrelated. An rc file whose final
  // command exits non-zero (a version manager complaining about a missing
  // .nvmrc) still printed the environment first. Worse, an rc file that
  // backgrounds anything holding stdout open — an agent, a daemon, a
  // `&`-suffixed one-liner — keeps the pipe from reaching EOF, so the
  // runner only returns at its deadline, with the complete,
  // already-collected answer in hand. Discarding that would mean the
  // feature never works on exactly the machines whose rc files do the most.
  const parsed = parseShellEnvOutput(result.stdout, beginMarker, endMarker);
  if (!parsed) {
    if (result.timedOut) return skip(`${shell} did not answer within ${timeoutMs}ms`);
    if (result.code !== 0) return skip(`${shell} exited ${result.code ?? 'on a signal'}`);
    return skip(`${shell} printed no readable environment`);
  }

  return { shell, ...mergeShellEnv(env, parsed) };
}
