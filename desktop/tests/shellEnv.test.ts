import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  DEFAULT_SHELL_ENV_TIMEOUT_MS,
  mergeShellEnv,
  parseShellEnvOutput,
  resolveShellEnv,
  type ShellRunRequest,
  type ShellRunResult,
  type ShellRunner,
} from '../src/main/shellEnv.js';

/**
 * Inheriting the user's terminal environment.
 *
 * Every interesting case here is something a real machine does and a test
 * machine will not: an rc file that prints a banner, an environment with a
 * newline inside a value, an `env` with no -0, a shell that hangs. They are
 * all stated as data against the injected runner, which is why this file
 * asserts behaviour that would otherwise only ever be discovered in a bug
 * report from someone whose .zshrc prints a fortune.
 *
 * The other half is what must NOT be inherited. A shell spawned to answer
 * a question describes itself in its own environment (PWD, SHLVL, TERM),
 * and one of those variables — ELECTRON_RUN_AS_NODE — silently changes
 * what every Electron process we spawn afterwards *is*.
 */

/** The two markers resolveShellEnv put in the script it asked the shell to run. */
function markersOf(request: ShellRunRequest): { begin: string; end: string } {
  const script = request.args.at(-1) ?? '';
  const found = [...script.matchAll(/printf '%s' "([^"]+)"/g)].map((m) => m[1] ?? '');
  if (found.length !== 2 || !found[0] || !found[1]) {
    throw new Error(`expected two markers in script: ${script}`);
  }
  if (found[0] === found[1]) throw new Error(`the markers must differ: ${script}`);
  return { begin: found[0], end: found[1] };
}

/** A shell that prints `vars` the way `env -0` would, markers included. */
function shellPrinting(
  vars: Record<string, string>,
  options: { before?: string; after?: string } = {},
): { run: ShellRunner; requests: ShellRunRequest[] } {
  const requests: ShellRunRequest[] = [];
  const run: ShellRunner = (request) => {
    requests.push(request);
    const { begin, end } = markersOf(request);
    // What dash does: `_` is the previous command's last argument — our
    // begin marker — and it is exported, so `env` prints it. A parser that
    // looked for the same marker twice would end the body right here.
    const body = Object.entries({ ...vars, _: begin })
      .map(([key, value]) => `${key}=${value}\0`)
      .join('');
    return Promise.resolve({
      stdout: `${options.before ?? ''}${begin}${body}${end}${options.after ?? ''}`,
      code: 0,
    });
  };
  return { run, requests };
}

function failingShell(result: Partial<ShellRunResult>): ShellRunner {
  return () => Promise.resolve({ stdout: '', code: 1, ...result });
}

describe('parseShellEnvOutput', () => {
  it('ignores everything outside the markers', () => {
    // An interactive login shell prints whatever the user's rc files print:
    // a MOTD before our first printf, a "goodbye" after our last one. Both
    // would otherwise be parsed as environment, or break the parse.
    const stdout = [
      'Last login: Tue Sep  9 08:12:03\n',
      'nvm: no .nvmrc found, using default\n',
      'MARK',
      'PATH=/opt/bin\0HOME=/home/tester\0',
      'MARK',
      '\nlogout\nSaving session…\n',
    ].join('');

    expect(parseShellEnvOutput(stdout, 'MARK', 'MARK')).toEqual({
      PATH: '/opt/bin',
      HOME: '/home/tester',
    });
  });

  it('keeps a value that contains newlines, which is why env -0 is used', () => {
    // An exported shell function and an ssh key both look like this, and a
    // line-based parse turns them into three bogus variables.
    const fn = 'BASH_FUNC_ll%%=() {  ls -l "$@"\n}';
    const stdout = `M${fn}\0LANG=en_GB.UTF-8\0M`;

    expect(parseShellEnvOutput(stdout, 'M', 'M')).toEqual({
      'BASH_FUNC_ll%%': '() {  ls -l "$@"\n}',
      LANG: 'en_GB.UTF-8',
    });
  });

  it('falls back to lines when the shell had no env -0 to run', () => {
    // BSD and busybox `env` have no -0, so the script's `printenv` branch
    // runs and the records arrive newline-separated.
    const stdout = 'M' + 'PATH=/usr/bin:/bin\nSHELL=/bin/sh\nEDITOR=vi\n' + 'M';

    expect(parseShellEnvOutput(stdout, 'M', 'M')).toEqual({
      PATH: '/usr/bin:/bin',
      SHELL: '/bin/sh',
      EDITOR: 'vi',
    });
  });

  it('drops the continuation lines of a multi-line value in the fallback', () => {
    // Nothing can reattach them — there is no record separator to go on —
    // so the tail is lost rather than invented as new variables.
    const stdout = 'M' + 'KEY=-----BEGIN-----\nAAAA BBBB\n-----END-----\nLANG=C\n' + 'M';

    expect(parseShellEnvOutput(stdout, 'M', 'M')).toEqual({ KEY: '-----BEGIN-----', LANG: 'C' });
  });

  it('reads past a `_` record that carries the begin marker (dash exports it)', () => {
    // dash and busybox ash export `_`, and at the moment `env` runs it holds
    // the last argument of the previous command: the begin marker. With a
    // single marker used twice the body would end at that record and PATH,
    // printed later, would never be seen.
    const stdout = 'M1' + 'HOME=/home/tester\0_=M1\0PATH=/opt/bin:/usr/bin\0' + 'M2';
    expect(parseShellEnvOutput(stdout, 'M1', 'M2')).toEqual({
      HOME: '/home/tester',
      _: 'M1',
      PATH: '/opt/bin:/usr/bin',
    });
  });

  it('is null when a marker is missing, so a failed shell is not read as an empty env', () => {
    expect(parseShellEnvOutput('nothing here', 'M', 'M')).toBeNull();
    expect(parseShellEnvOutput('MPATH=/usr/bin', 'M', 'M')).toBeNull();
    expect(parseShellEnvOutput('', 'M', 'M')).toBeNull();
  });

  it('skips records that are not KEY=VALUE at all', () => {
    const stdout = 'M' + 'noequals\0=novalue\0OK=1\0' + 'M';
    expect(parseShellEnvOutput(stdout, 'M', 'M')).toEqual({ OK: '1' });
  });
});

describe('mergeShellEnv', () => {
  it('takes the shell’s answer for a variable we disagree on', () => {
    const merged = mergeShellEnv(
      { EDITOR: 'nano', LANG: 'C' },
      { EDITOR: 'nvim', NVM_DIR: '/home/tester/.nvm' },
    );

    expect(merged.env['EDITOR']).toBe('nvim');
    expect(merged.env['NVM_DIR']).toBe('/home/tester/.nvm');
    // Ours survives when the shell says nothing about it.
    expect(merged.env['LANG']).toBe('C');
    expect(merged.changed.sort()).toEqual(['EDITOR', 'NVM_DIR']);
  });

  it('unions PATH with the shell first and our own entries kept behind it', () => {
    // Order is the entire point: the version manager's node has to win
    // over /usr/bin/node, and nothing that used to resolve may stop.
    const merged = mergeShellEnv(
      { PATH: '/usr/local/bin:/usr/bin:/bin:/opt/electron/helpers' },
      { PATH: '/home/tester/.nvm/versions/node/v22.22.1/bin:/usr/local/bin:/usr/bin:/bin' },
    );

    expect(merged.env['PATH']).toBe(
      '/home/tester/.nvm/versions/node/v22.22.1/bin:/usr/local/bin:/usr/bin:/bin:/opt/electron/helpers',
    );
    expect(merged.pathAdded).toEqual(['/home/tester/.nvm/versions/node/v22.22.1/bin']);
    expect(merged.changed).toContain('PATH');
  });

  it('reports no change when the shell agrees with us entirely', () => {
    const current = { PATH: '/usr/bin:/bin', LANG: 'C' };
    const merged = mergeShellEnv(current, { PATH: '/usr/bin:/bin', LANG: 'C' });

    expect(merged.changed).toEqual([]);
    expect(merged.pathAdded).toEqual([]);
    expect(merged.env).toEqual(current);
  });

  it('never copies a variable that describes the probe shell itself', () => {
    // ELECTRON_RUN_AS_NODE is the dangerous one: inherited into every child
    // we spawn, it turns an Electron process into a bare Node one.
    const merged = mergeShellEnv(
      { PWD: '/opt/collimate', TERM: 'dumb', LANG: 'C' },
      {
        _: '/usr/bin/env',
        SHLVL: '3',
        PWD: '/home/tester',
        OLDPWD: '/tmp',
        TERM: 'xterm-256color',
        COLUMNS: '80',
        LINES: '24',
        ELECTRON_RUN_AS_NODE: '1',
        ELECTRON_NO_ATTACH_CONSOLE: '1',
        LANG: 'en_GB.UTF-8',
      },
    );

    expect(merged.changed).toEqual(['LANG']);
    expect(merged.env['PWD']).toBe('/opt/collimate');
    expect(merged.env['TERM']).toBe('dumb');
    for (const key of ['_', 'SHLVL', 'OLDPWD', 'COLUMNS', 'LINES', 'ELECTRON_RUN_AS_NODE', 'ELECTRON_NO_ATTACH_CONSOLE']) {
      expect(merged.env[key]).toBeUndefined();
    }
  });

  it('never takes the app’s own configuration from the shell', () => {
    // COLLIMATE_* is the app talking to itself. Everything here is decided
    // before this merge runs — the profile at module load, the opt-out
    // before the probe, the server target when the mode is chosen — so a
    // value arriving from an rc file could only contradict a decision
    // already made, and would do it differently depending on whether the
    // app was started from a menu or a terminal.
    const merged = mergeShellEnv(
      { COLLIMATE_SHELL_ENV: '1', LANG: 'C' },
      {
        COLLIMATE_SHELL_ENV: '0',
        COLLIMATE_APP_PROFILE: 'stable',
        COLLIMATE_CONNECT_URL: 'http://someone-elses-box:5001',
        LANG: 'en_GB.UTF-8',
      },
    );

    expect(merged.changed).toEqual(['LANG']);
    expect(merged.env['COLLIMATE_SHELL_ENV']).toBe('1');
    expect(merged.env['COLLIMATE_APP_PROFILE']).toBeUndefined();
    expect(merged.env['COLLIMATE_CONNECT_URL']).toBeUndefined();
  });

  it('leaves PATH alone when the shell reported none', () => {
    const merged = mergeShellEnv({ PATH: '/usr/bin' }, { LANG: 'C' });
    expect(merged.env['PATH']).toBe('/usr/bin');
    expect(merged.changed).toEqual(['LANG']);
  });
});

describe('resolveShellEnv', () => {
  const env = { SHELL: '/bin/zsh', PATH: '/usr/bin:/bin' };

  it('keeps the eight-second login-shell cap and merges its marked answer __F1048', async () => {
    const { run, requests } = shellPrinting({
      PATH: '/home/tester/.nvm/versions/node/v22.22.1/bin:/usr/bin:/bin',
      NVM_DIR: '/home/tester/.nvm',
    });

    const result = await resolveShellEnv({ platform: 'linux', env, run });

    expect(result?.shell).toBe('/bin/zsh');
    expect(result?.pathAdded).toEqual(['/home/tester/.nvm/versions/node/v22.22.1/bin']);
    expect(result?.env['NVM_DIR']).toBe('/home/tester/.nvm');
    // -i matters as much as -l: version managers are conventionally set up
    // in .bashrc/.zshrc, which a login-only shell never reads.
    expect(requests[0]?.args[0]).toBe('-ilc');
    expect(DEFAULT_SHELL_ENV_TIMEOUT_MS).toBe(8_000);
    expect(requests[0]?.timeoutMs).toBe(8_000);
  });

  it('reads its answer out of a noisy rc file’s output', async () => {
    const { run } = shellPrinting(
      { PATH: '/opt/tools/bin:/usr/bin:/bin' },
      { before: 'Welcome to zsh!\n', after: '\n[oh-my-zsh] update available\n' },
    );

    const result = await resolveShellEnv({ platform: 'linux', env, run });

    expect(result?.pathAdded).toEqual(['/opt/tools/bin']);
  });

  const skips: [string, Parameters<typeof resolveShellEnv>[0], RegExp][] = [
    ['Windows has no login-shell convention', { platform: 'win32', env, run: failingShell({}) }, /Windows/],
    ['there is no $SHELL to ask', { platform: 'darwin', env: { PATH: '/usr/bin' }, run: failingShell({}) }, /SHELL/],
    [
      'the user opted out',
      { platform: 'linux', env: { ...env, COLLIMATE_SHELL_ENV: '0' }, run: failingShell({}) },
      /COLLIMATE_SHELL_ENV/,
    ],
    ['the shell never answered', { platform: 'linux', env, run: failingShell({ timedOut: true, code: null }) }, /within/],
    ['an rc file exited non-zero', { platform: 'linux', env, run: failingShell({ code: 127 }) }, /exited 127/],
    [
      'the output had no markers in it',
      { platform: 'linux', env, run: () => Promise.resolve({ stdout: 'PATH=/usr/bin\n', code: 0 }) },
      /readable/,
    ],
    [
      'the shell could not be spawned at all',
      {
        platform: 'linux',
        env,
        run: () => Promise.reject(new Error('spawn /bin/zsh ENOENT')),
      },
      /ENOENT/,
    ],
  ];

  for (const [because, options, reason] of skips) {
    it(`returns null, with a reason, when ${because}`, async () => {
      // Null means "keep the environment we already have", which is exactly
      // the behaviour this feature replaced — so every failure mode has to
      // land here rather than throwing on the startup path.
      const reasons: string[] = [];
      const result = await resolveShellEnv({ ...options, onSkip: (r) => reasons.push(r) });

      expect(result).toBeNull();
      expect(reasons).toHaveLength(1);
      expect(reasons[0]).toMatch(reason);
    });
  }

  it('keeps an answer the shell printed before it had to be killed', async () => {
    // An rc file that backgrounds anything holding stdout open — an agent,
    // a daemon, a `&`-suffixed one-liner — keeps the pipe from reaching
    // EOF, so execFile's callback fires only at the timeout, long after the
    // environment was printed. Throwing that away would mean the feature
    // fails on exactly the machines whose rc files do the most.
    const { run } = shellPrinting({ PATH: '/opt/tools/bin:/usr/bin:/bin' });
    const printedThenHung: ShellRunner = async (request) => ({
      ...(await run(request)),
      code: null,
      timedOut: true,
    });

    const reasons: string[] = [];
    const result = await resolveShellEnv({
      platform: 'linux',
      env,
      run: printedThenHung,
      onSkip: (reason) => reasons.push(reason),
    });

    expect(result?.pathAdded).toEqual(['/opt/tools/bin']);
    expect(reasons).toEqual([]);
  });

  it('keeps an answer whose rc file then exited non-zero', async () => {
    // The exit status describes the last thing the rc files did — a version
    // manager grumbling about a missing .nvmrc — not whether the
    // environment was printed, which happened first either way.
    const { run } = shellPrinting({ NVM_DIR: '/home/tester/.nvm' });
    const printedThenFailed: ShellRunner = async (request) => ({
      ...(await run(request)),
      code: 127,
    });

    const result = await resolveShellEnv({ platform: 'linux', env, run: printedThenFailed });

    expect(result?.env['NVM_DIR']).toBe('/home/tester/.nvm');
  });

  it('still gives up when the shell was killed before it printed anything', async () => {
    // The markers, not the exit status, are what says the shell answered:
    // a banner is not an environment, and half a banner is not one either.
    const reasons: string[] = [];
    const result = await resolveShellEnv({
      platform: 'linux',
      env,
      run: failingShell({ stdout: 'Welcome to zsh!\n', code: null, timedOut: true }),
      onSkip: (reason) => reasons.push(reason),
    });

    expect(result).toBeNull();
    expect(reasons[0]).toMatch(/within/);
  });

  it('never asks the shell anything on the paths that are decided up front', async () => {
    let asked = 0;
    const run: ShellRunner = () => {
      asked += 1;
      return Promise.resolve({ stdout: '', code: 0 });
    };

    await resolveShellEnv({ platform: 'win32', env, run });
    await resolveShellEnv({ platform: 'linux', env: {}, run });
    await resolveShellEnv({ platform: 'linux', env: { ...env, COLLIMATE_SHELL_ENV: '0' }, run });

    expect(asked).toBe(0);
  });

  // The tests that run a real shell: the injected runner proves the logic,
  // but only these prove the script is something a shell will actually
  // execute, that the default runner reads its output back, and — the part
  // a fake cannot show — that the deadline is ours and not the shell's.
  // /bin/sh is dash on Debian-family machines, the shell that exports `_`
  // and ignores SIGTERM, which is exactly why it is the one to use. An
  // interactive sh reads $ENV, so an rc file can be handed in per test.
  describe.skipIf(process.platform === 'win32')('against a real /bin/sh', () => {
    const rcDirs: string[] = [];
    afterEach(() => {
      for (const dir of rcDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
    });
    function rcFile(contents: string): string {
      const dir = mkdtempSync(path.join(os.tmpdir(), 'collimate-shellenv-'));
      rcDirs.push(dir);
      const file = path.join(dir, 'rc.sh');
      writeFileSync(file, contents, 'utf8');
      return file;
    }
    const baseEnv = { ...process.env, SHELL: '/bin/sh', COLLIMATE_SHELL_ENV: '1' };

    it(
      'reads the environment the rc file built, PATH included',
      async () => {
        const rc = rcFile(
          'SHELL_ENV_CANARY=from-rc; export SHELL_ENV_CANARY\n' +
            'PATH="/opt/canary/bin:$PATH"; export PATH\n',
        );
        const result = await resolveShellEnv({
          platform: process.platform,
          env: { ...baseEnv, ENV: rc, SHELL_ENV_CANARY: 'from-launcher' },
        });

        expect(result).not.toBeNull();
        // Both prove the parse ran past `_`: the canary is set late in the
        // environment and PATH is what the whole feature exists to inherit.
        expect(result?.env['SHELL_ENV_CANARY']).toBe('from-rc');
        expect(result?.changed).toContain('SHELL_ENV_CANARY');
        expect(result?.pathAdded).toEqual(['/opt/canary/bin']);
        // Whatever the merge did, the PATH we came in with must still resolve.
        for (const entry of (process.env['PATH'] ?? '').split(':').filter(Boolean)) {
          expect(result?.env['PATH']?.split(':')).toContain(entry);
        }
      },
      20_000,
    );

    it(
      'keeps the answer when the rc file leaves a child holding stdout',
      async () => {
        // `sleep 5 &` inherits the pipe, so stdout never reaches EOF while
        // it runs; the shell itself exits at once and the exit event ends
        // the wait — well inside the deadline, with the environment intact.
        const rc = rcFile('sleep 5 &\nSHELL_ENV_CANARY=held; export SHELL_ENV_CANARY\n');
        const started = Date.now();
        const result = await resolveShellEnv({
          platform: process.platform,
          env: { ...baseEnv, ENV: rc },
          timeoutMs: 4_000,
        });

        expect(result?.env['SHELL_ENV_CANARY']).toBe('held');
        expect(Date.now() - started).toBeLessThan(3_000);
      },
      20_000,
    );

    // Linux only: macOS ps has no `sid` keyword.
    it.skipIf(process.platform !== 'linux')(
      'runs the probe shell in a session of its own, away from any launching terminal',
      async () => {
        // An interactive shell on the launching terminal takes its foreground
        // group, and keeps it if killed at the deadline; a session leader
        // has no controlling terminal to take.
        const rc = rcFile(
          "SHELL_ENV_SID=$(ps -o sid= -p $$ | tr -d ' '); SHELL_ENV_PID=$$\n" +
            'export SHELL_ENV_SID SHELL_ENV_PID\n',
        );
        const result = await resolveShellEnv({
          platform: process.platform,
          env: { ...baseEnv, ENV: rc },
        });

        expect(result?.env['SHELL_ENV_PID']).toMatch(/^\d+$/);
        expect(result?.env['SHELL_ENV_SID']).toBe(result?.env['SHELL_ENV_PID']);
      },
      20_000,
    );

    it('waits for a real shell logout trap after its complete marked answer __F1048', async () => {
      const rc = rcFile("trap 'sleep 0.25' EXIT\nSHELL_ENV_LOGOUT_CANARY=present; export SHELL_ENV_LOGOUT_CANARY\n");
      const started = Date.now();
      const result = await resolveShellEnv({ platform: process.platform, env: { ...baseEnv, ENV: rc } });
      expect(result?.env['SHELL_ENV_LOGOUT_CANARY']).toBe('present');
      expect(Date.now() - started).toBeGreaterThanOrEqual(200);
    });

    it(
      'gives up at its own deadline when the rc file blocks, even under dash',
      async () => {
        // dash ignores SIGTERM, so execFile's timeout would have waited out
        // the whole `sleep`; the runner's SIGKILL + grace must not.
        const rc = rcFile('sleep 20\n');
        const reasons: string[] = [];
        const started = Date.now();
        const result = await resolveShellEnv({
          platform: process.platform,
          env: { ...baseEnv, ENV: rc },
          timeoutMs: 500,
          onSkip: (reason) => reasons.push(reason),
        });

        expect(result).toBeNull();
        expect(reasons[0]).toMatch(/within 500ms/);
        expect(Date.now() - started).toBeLessThan(3_000);
      },
      20_000,
    );
  });
});
