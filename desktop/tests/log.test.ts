import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { appendFileSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { createLineSplitter } from '../src/main/childLines.js';
import { closeProcessLog, MemoryLog, openProcessLog, RunLog, tailFile, type LogSink } from '../src/main/log.js';

/**
 * The local server's output: a real child writes to the process log file
 * through the descriptor openProcessLog hands out (never a pipe), and the
 * shell follows that file into its run log line by line.
 */

async function scratch(): Promise<string> {
  return mkdtemp(path.join(os.tmpdir(), 'desktop-log-'));
}

/** Run `script` with stdout and stderr on the process log, following it into `log`. */
async function childOutput(dir: string, log: LogSink, script: string): Promise<void> {
  const file = path.join(dir, 'server-process.log');
  const fd = openProcessLog(file);
  const stop = tailFile(file, (line) => log.server(line), { intervalMs: 10 });
  const child = spawn(process.execPath, ['-e', script], { stdio: ['ignore', fd, fd] });
  closeProcessLog(fd);
  const [code] = await once(child, 'close');
  expect(code).toBe(0);
  stop();
}

describe('production child lines __F1030', () => {
  it('joins split chunks, keeps blank lines and flushes final stdout/stderr partials to disk __F1030', async () => {
    const dir = await scratch();
    const file = path.join(dir, 'server.log');
    const log = new RunLog(file);
    try {
      await childOutput(dir, log, `process.stdout.write('fir'); setTimeout(() => { process.stdout.write('st\\r\\n\\nlast'); process.stderr.write('\\nerror-tail'); }, 30);`);
      expect(log.tail()).toContain('first');
      expect(log.tail()).toContain('');
      expect(log.tail()).toContain('last');
      expect(log.tail()).toContain('error-tail');
      log.close();
      await expect.poll(async () => readFile(file, 'utf8')).toContain('first\n\n');
      expect(await readFile(file, 'utf8')).toContain('error-tail\n');
    } finally {
      log.close();
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('caps a newline-free partial and resumes line splitting without losing bytes __F1030', () => {
    const lines: string[] = [];
    const splitter = createLineSplitter((line) => lines.push(line));
    splitter.push('x'.repeat(8193));
    splitter.push('end\n');
    splitter.end();
    expect(lines.join('')).toBe('x'.repeat(8193) + 'end');
    expect(lines).toHaveLength(2);
  });
});

it('memory sink uses production chunk joining, blank lines and tail flush __F1030', async () => {
  const dir = await scratch();
  try {
    const log = new MemoryLog();
    await childOutput(dir, log, `process.stdout.write('fir'); setTimeout(() => process.stdout.write('st\\r\\n\\nlast'), 30);`);
    expect(log.tail()).toEqual(['first', '', 'last']);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

describe('the process log file (MS-04)', () => {
  it('rotates the previous run to .1 and appends from a fresh file (MS-04)', async () => {
    const dir = await scratch();
    try {
      const file = path.join(dir, 'logs', 'server-process.log');
      closeProcessLog(openProcessLog(file));
      writeFileSync(file, 'previous run\n');
      const fd = openProcessLog(file);
      closeProcessLog(fd);
      expect(readFileSync(`${file}.1`, 'utf8')).toBe('previous run\n');
      expect(readFileSync(file, 'utf8')).toBe('');
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('a writer whose follower is gone keeps writing: no pipe to break (MS-04)', async () => {
    const dir = await scratch();
    try {
      const file = path.join(dir, 'server-process.log');
      const fd = openProcessLog(file);
      const stop = tailFile(file, () => undefined, { intervalMs: 10 });
      const child = spawn(process.execPath, ['-e', `setTimeout(() => { process.stdout.write('after\\n'); }, 100);`], {
        stdio: ['ignore', fd, fd],
      });
      closeProcessLog(fd);
      // The shell stops following (a released server) before the write.
      stop();
      const [code] = await once(child, 'close');
      expect(code).toBe(0);
      expect(readFileSync(file, 'utf8')).toBe('after\n');
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('follows from the end for an adopted server, waits for a missing file and rereads a rotated one (MS-04)', async () => {
    const dir = await scratch();
    try {
      const file = path.join(dir, 'server-process.log');
      writeFileSync(file, 'from the launch that started it\n');
      const lines: string[] = [];
      const stop = tailFile(file, (line) => lines.push(line), { fromEnd: true, intervalMs: 10 });
      appendFileSync(file, 'new line\n');
      await expect.poll(() => lines).toEqual(['new line']);
      // Truncated in place (rotated): read again from its start.
      writeFileSync(file, 'fresh\n');
      await expect.poll(() => lines).toEqual(['new line', 'fresh']);
      stop();

      const missing = path.join(dir, 'later.log');
      const later: string[] = [];
      const stopLater = tailFile(missing, (line) => later.push(line), { intervalMs: 10 });
      expect(existsSync(missing)).toBe(false);
      writeFileSync(missing, 'appeared\npartial');
      await expect.poll(() => later).toEqual(['appeared']);
      stopLater();
      expect(later).toEqual(['appeared', 'partial']);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
