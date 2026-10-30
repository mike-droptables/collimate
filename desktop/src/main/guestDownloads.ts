import { randomUUID } from 'node:crypto';
import { closeSync, lstatSync, mkdirSync, openSync, readdirSync, unlinkSync } from 'node:fs';
import path from 'node:path';
import type { GuestEvent } from '../shared/bridge.js';

export interface DownloadItemLike {
  getFilename(): string;
  getTotalBytes(): number;
  getReceivedBytes(): number;
  setSavePath(filePath: string): void;
  cancel(): void;
  on(event: 'updated', listener: () => void): unknown;
  once(event: 'done', listener: (event: unknown, state: string) => void): unknown;
}
export interface DownloadSessionLike {
  on(event: 'will-download', listener: (event: { preventDefault(): void }, item: DownloadItemLike, contents: unknown) => void): unknown;
}
type DownloadData = Extract<GuestEvent, { kind: 'download' }>['data'];
interface Owner { emit(data: DownloadData): void; transfers: Set<() => void> }

/** One application-wide policy, with routing by actual managed contents identity. */
export class GuestDownloads {
  private readonly sessions = new WeakSet<object>();
  private readonly owners = new Map<unknown, Owner>();
  private active = 0;
  constructor(private readonly downloadsDirectory: () => string) {}

  register(session: DownloadSessionLike, contents: unknown, emit: Owner['emit']): () => void {
    const previous = this.owners.get(contents);
    if (previous) {
      this.owners.delete(contents);
      for (const cancel of [...previous.transfers]) cancel();
    }
    const owner: Owner = { emit, transfers: new Set() };
    this.owners.set(contents, owner);
    if (!this.sessions.has(session)) {
      this.sessions.add(session);
      session.on('will-download', (event, item, initiator) => this.start(event, item, initiator));
    }
    return () => {
      if (this.owners.get(contents) !== owner) return;
      this.owners.delete(contents);
      for (const cancel of [...owner.transfers]) cancel();
    };
  }

  private start(event: { preventDefault(): void }, item: DownloadItemLike, contents: unknown): void {
    const owner = this.owners.get(contents);
    if (!owner) { event.preventDefault(); return; }
    const original = path.basename(item.getFilename().replaceAll('\\', '/'));
    const suffix = original.replace(/[^a-zA-Z0-9._ -]/g, '_').slice(-120) || 'download';
    const fileName = `${randomUUID()}-${suffix}`;
    const emit = (data: DownloadData): void => {
      if (this.owners.get(contents) === owner) owner.emit(data);
    };
    const refuse = (reason: string): void => {
      emit({ status: 'refused', fileName, reason });
      event.preventDefault();
    };
    const limit = 100 * 1024 * 1024;
    if (this.active >= 4) { refuse('Too many active downloads'); return; }
    if (item.getTotalBytes() > limit) { refuse('Download exceeds 100 MiB'); return; }
    let destination: string;
    try {
      const directory = path.join(this.downloadsDirectory(), 'Collimate');
      mkdirSync(directory, { recursive: true });
      const info = lstatSync(directory);
      if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('Unsafe destination');
      if (readdirSync(directory).length >= 100) { refuse('Download directory has 100 entries'); return; }
      destination = path.join(directory, fileName);
      closeSync(openSync(destination, 'wx', 0o600));
    } catch { refuse('Download destination unavailable'); return; }

    this.active++;
    let finished = false;
    let cancelling = false;
    const finish = (completed: boolean, reason?: string): void => {
      if (finished) return;
      finished = true;
      this.active--;
      owner.transfers.delete(cancel);
      if (!completed) {
        try { unlinkSync(destination); } catch { /* Already removed or not created by Chromium. */ }
      }
      emit({ status: completed ? 'completed' : 'failed', fileName, ...(reason ? { reason } : {}) });
    };
    const cancel = (): void => {
      if (finished || cancelling) return;
      cancelling = true;
      try { item.cancel(); } catch { finish(false, 'Download cancelled'); }
    };
    owner.transfers.add(cancel);
    item.once('done', (_event, state) => {
      const completed = !cancelling && state === 'completed' && item.getReceivedBytes() <= limit;
      finish(completed, completed ? undefined : 'Download interrupted');
    });
    item.on('updated', () => {
      if (!finished && (item.getReceivedBytes() > limit || item.getTotalBytes() > limit)) cancel();
    });
    try {
      item.setSavePath(destination);
      emit({ status: 'started', fileName });
    } catch { cancel(); }
  }
}
