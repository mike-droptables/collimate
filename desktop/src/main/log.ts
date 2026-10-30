import { createLineSplitter } from './childLines.js';
import {
  closeSync,
  createWriteStream,
  mkdirSync,
  openSync,
  readSync,
  renameSync,
  statSync,
  type WriteStream,
} from 'node:fs';
import path from 'node:path';
import { StringDecoder } from 'node:string_decoder';

/**
 * What the rest of the shell needs from a log. Narrow on purpose: the
 * ServerController takes one of these, so a test can hand it an in-memory
 * implementation and assert on the tail without touching disk.
 */
export interface LogSink {
  /** A line from the shell itself (prefixed `[shell]` on disk). */
  shell(message: string): void;
  /** A line that came out of the server child process. */
  server(line: string): void;
  /** Last `n` lines, oldest first. Feeds the splash and the failure UI. */
  tail(n?: number): string[];
  /** Subscribe to lines as they land. Returns an unsubscribe function. */
  onLine(handler: (line: string) => void): () => void;
  close(): void;
}

const DEFAULT_RING = 400;
const DEFAULT_TAIL = 25;

/**
 * `<userData>/logs/server.log`, truncated once per run.
 *
 * Truncate-per-run rather than rotate: this log exists to answer "why
 * didn't it start *this* time", and a first `uvx` run can emit thousands
 * of resolver lines. History across runs isn't worth the rotation
 * machinery — the last run is the one anyone ever asks about.
 */
export class RunLog implements LogSink {
  private readonly ring: string[] = [];
  private readonly ringSize: number;
  private readonly handlers = new Set<(line: string) => void>();
  private stream: WriteStream | null = null;
  private closed = false;

  constructor(
    readonly filePath: string,
    ringSize: number = DEFAULT_RING,
  ) {
    this.ringSize = ringSize;
    try {
      mkdirSync(path.dirname(filePath), { recursive: true });
      // 'w' — truncate. See the class comment.
      this.stream = createWriteStream(filePath, { flags: 'w' });
      // A disk error must never take the app down; we still have the ring
      // buffer, which is what the UI actually reads.
      this.stream.on('error', () => {
        this.stream = null;
      });
    } catch {
      this.stream = null;
    }
  }

  /** `<userData>/logs/server.log`. */
  static inUserData(userDataDir: string): RunLog {
    return new RunLog(path.join(userDataDir, 'logs', 'server.log'));
  }

  shell(message: string): void {
    this.push(`[shell] ${message}`);
  }

  server(line: string): void {
    this.push(line);
  }

  tail(n: number = DEFAULT_TAIL): string[] {
    return this.ring.slice(Math.max(0, this.ring.length - n));
  }

  onLine(handler: (line: string) => void): () => void {
    this.handlers.add(handler);
    return () => {
      this.handlers.delete(handler);
    };
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.handlers.clear();
    this.stream?.end();
    this.stream = null;
  }

  private push(line: string): void {
    if (this.closed) return;
    this.ring.push(line);
    if (this.ring.length > this.ringSize) {
      this.ring.splice(0, this.ring.length - this.ringSize);
    }
    this.stream?.write(`${line}\n`);
    for (const handler of this.handlers) {
      try {
        handler(line);
      } catch {
        // A bad subscriber (e.g. a splash window that just closed) must
        // not break logging for everyone else.
      }
    }
  }
}

/** In-memory only. Used as the default when no log is supplied, and by tests. */
export class MemoryLog implements LogSink {
  readonly lines: string[] = [];
  private readonly handlers = new Set<(line: string) => void>();

  shell(message: string): void {
    this.push(`[shell] ${message}`);
  }

  server(line: string): void {
    this.push(line);
  }

  tail(n: number = DEFAULT_TAIL): string[] {
    return this.lines.slice(Math.max(0, this.lines.length - n));
  }

  onLine(handler: (line: string) => void): () => void {
    this.handlers.add(handler);
    return () => {
      this.handlers.delete(handler);
    };
  }

  close(): void {
    this.handlers.clear();
  }

  private push(line: string): void {
    this.lines.push(line);
    for (const handler of this.handlers) handler(line);
  }
}

// ---------------------------------------------------------------------------
// The server process's own output file
// ---------------------------------------------------------------------------

/** `<userData>/logs/server-process.log`: the local server's stdout and stderr. */
export function processLogPath(userDataDir: string): string {
  return path.join(userDataDir, 'logs', 'server-process.log');
}

/**
 * Open the server process's output file for a new spawn, as a file
 * descriptor the child's stdout and stderr are pointed at.
 *
 * A file, never a pipe: a server the shell leaves running on quit ("keep
 * the server running") would otherwise write into a pipe whose reader is
 * gone — EPIPE, a BrokenPipeError out of every print — and whether a
 * server outlives this shell is only decided at quit, long after the spawn.
 * The previous run's file is rotated to `.1` first, so the file holds one
 * server's output; it is opened in append mode so the child's writes land
 * at the end whatever the shell does with its own descriptor.
 */
export function openProcessLog(file: string): number {
  mkdirSync(path.dirname(file), { recursive: true });
  try {
    renameSync(file, `${file}.1`);
  } catch {
    // No previous file: the first spawn in this data dir.
  }
  return openSync(file, 'a', 0o600);
}

/** Close a descriptor openProcessLog handed out; the child keeps its own copy. */
export function closeProcessLog(fd: number): void {
  try {
    closeSync(fd);
  } catch {
    // Already closed.
  }
}

export interface TailOptions {
  /**
   * Start at the file's current end rather than its beginning: an adopted
   * server's earlier output belongs to the run that started it.
   */
  fromEnd?: boolean;
  /** How often the file is checked for new bytes. */
  intervalMs?: number;
}

/** Most bytes read in one tick, so a burst cannot stall the main process. */
const TAIL_CHUNK_BYTES = 256 * 1024;
const TAIL_INTERVAL_MS = 250;

/**
 * Follow `file` like `tail -F`, line by line (the same splitting the shell
 * log has always used): new bytes are read every `intervalMs`, a file that
 * does not exist yet is waited for, and one that was truncated or replaced
 * is read again from its start. Returns a stop function, which reads what
 * is left and flushes the last partial line.
 */
export function tailFile(file: string, onLine: (line: string) => void, options: TailOptions = {}): () => void {
  const splitter = createLineSplitter(onLine);
  const decoder = new StringDecoder('utf8');
  let offset = 0;
  let inode: number | null = null;
  let stopped = false;

  const current = (): { size: number; ino: number } | null => {
    try {
      const stat = statSync(file);
      return { size: stat.size, ino: stat.ino };
    } catch {
      return null;
    }
  };

  if (options.fromEnd) {
    const stat = current();
    if (stat) {
      offset = stat.size;
      inode = stat.ino;
    }
  }

  /** Read one chunk of new bytes; returns how many were read. */
  const drain = (): number => {
    const stat = current();
    if (!stat) return 0;
    if (inode !== null && (stat.ino !== inode || stat.size < offset)) {
      // Rotated or truncated: the new file starts from nothing.
      offset = 0;
    }
    inode = stat.ino;
    if (stat.size <= offset) return 0;
    let fd: number;
    try {
      fd = openSync(file, 'r');
    } catch {
      return 0;
    }
    try {
      const buffer = Buffer.alloc(Math.min(TAIL_CHUNK_BYTES, stat.size - offset));
      const read = readSync(fd, buffer, 0, buffer.length, offset);
      offset += read;
      if (read > 0) splitter.push(decoder.write(buffer.subarray(0, read)));
      return read;
    } catch {
      // A transient read error is retried on the next tick.
      return 0;
    } finally {
      closeSync(fd);
    }
  };

  const timer = setInterval(drain, options.intervalMs ?? TAIL_INTERVAL_MS);
  timer.unref?.();

  return () => {
    if (stopped) return;
    stopped = true;
    clearInterval(timer);
    while (drain() > 0) {
      // Read everything the file holds before the last partial is flushed.
    }
    splitter.push(decoder.end());
    splitter.end();
  };
}
