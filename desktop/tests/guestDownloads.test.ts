import { EventEmitter } from 'node:events';
import { mkdtempSync, mkdirSync, readdirSync, readFileSync, writeFileSync, existsSync, symlinkSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { GuestDownloads, type DownloadItemLike } from '../src/main/guestDownloads.js';

class Item extends EventEmitter implements DownloadItemLike {
  destination = '';
  received = 0;
  total = 10;
  cancelled = false;
  getFilename() { return '../../unsafe\\name.txt'; }
  getTotalBytes() { return this.total; }
  getReceivedBytes() { return this.received; }
  setSavePath(filePath: string) { this.destination = filePath; writeFileSync(filePath, 'partial'); }
  cancel() { this.cancelled = true; this.emit('done', {}, 'cancelled'); }
}
class Session extends EventEmitter {
  start(item: Item, contents: unknown) {
    const event = { prevented: false, preventDefault() { this.prevented = true; } };
    this.emit('will-download', event, item, contents);
    return event;
  }
}
let directory: string;
beforeEach(() => { directory = mkdtempSync(path.join(os.tmpdir(), 'guest-dl-')); });
afterEach(() => rmSync(directory, { recursive: true, force: true }));

function rig() {
  const policy = new GuestDownloads(() => directory);
  const session = new Session();
  const contents = {};
  const events: unknown[] = [];
  const dispose = policy.register(session, contents, (data) => events.push(data));
  return { policy, session, contents, events, dispose };
}

describe('F0994 silent download policy', () => {
  it('reserves a safe exclusive path and completes without removing existing files', () => {
    const r = rig();
    mkdirSync(path.join(directory, 'Collimate'));
    writeFileSync(path.join(directory, 'Collimate', 'existing.txt'), 'keep');
    const item = new Item();
    expect(r.session.start(item, r.contents).prevented).toBe(false);
    expect(path.dirname(item.destination)).toBe(path.join(directory, 'Collimate'));
    expect(path.basename(item.destination)).toMatch(/^[\da-f-]+-name.txt$/);
    item.emit('done', {}, 'completed');
    expect(readFileSync(item.destination, 'utf8')).toBe('partial');
    expect(readFileSync(path.join(directory, 'Collimate', 'existing.txt'), 'utf8')).toBe('keep');
    expect(r.events).toEqual([
      { status: 'started', fileName: path.basename(item.destination) },
      { status: 'completed', fileName: path.basename(item.destination) },
    ]);
  });

  it('routes by actual contents identity, installs once, and cancels unknown initiators', () => {
    const r = rig();
    const other = {};
    const events: unknown[] = [];
    r.policy.register(r.session, other, (event) => events.push(event));
    expect(r.session.listenerCount('will-download')).toBe(1);
    const item = new Item();
    expect(r.session.start(item, {}).prevented).toBe(true);
    expect(item.destination).toBe('');
    r.session.start(new Item(), other);
    expect(events).toHaveLength(1);
    expect(r.events).toHaveLength(0);
  });

  it('enforces four global transfers across sessions and releases interrupted reservations', () => {
    const r = rig();
    const otherSession = new Session();
    const otherContents = {};
    r.policy.register(otherSession, otherContents, () => {});
    const items = Array.from({ length: 4 }, () => new Item());
    items.forEach((item) => r.session.start(item, r.contents));
    expect(otherSession.start(new Item(), otherContents).prevented).toBe(true);
    items[0]!.emit('done', {}, 'interrupted');
    expect(existsSync(items[0]!.destination)).toBe(false);
    expect(otherSession.start(new Item(), otherContents).prevented).toBe(false);
  });

  it('counts existing entries and active reservations toward the 100-entry cap', () => {
    const r = rig();
    mkdirSync(path.join(directory, 'Collimate'));
    for (let i = 0; i < 99; i++) writeFileSync(path.join(directory, 'Collimate', `${i}`), 'keep');
    const item = new Item();
    r.session.start(item, r.contents);
    expect(readdirSync(path.join(directory, 'Collimate'))).toHaveLength(100);
    expect(r.session.start(new Item(), r.contents).prevented).toBe(true);
    item.emit('done', {}, 'interrupted');
    expect(readdirSync(path.join(directory, 'Collimate'))).toHaveLength(99);
  });

  it('rejects known oversize and cancels unknown-length progress beyond 100 MiB', () => {
    const r = rig();
    const known = new Item(); known.total = 100 * 1024 * 1024 + 1;
    expect(r.session.start(known, r.contents).prevented).toBe(true);
    expect(known.destination).toBe('');
    const unknown = new Item(); unknown.total = 0;
    r.session.start(unknown, r.contents);
    unknown.received = 100 * 1024 * 1024 + 1;
    unknown.emit('updated');
    expect(unknown.cancelled).toBe(true);
    expect(existsSync(unknown.destination)).toBe(false);
    expect(r.events.at(-1)).toMatchObject({ status: 'failed' });
  });

  it('waits for cancellation completion before removing bytes and releasing its slot', () => {
    const r = rig();
    const item = new Item();
    item.cancel = () => { item.cancelled = true; };
    r.session.start(item, r.contents);
    item.received = 100 * 1024 * 1024 + 1;
    item.emit('updated');
    expect(item.cancelled).toBe(true);
    expect(existsSync(item.destination)).toBe(true);
    writeFileSync(item.destination, 'final interrupted writer bytes');
    item.emit('done', {}, 'cancelled');
    expect(existsSync(item.destination)).toBe(false);
    expect(r.events.filter((event) => (event as {status:string}).status === 'failed')).toHaveLength(1);
  });

  it('refuses a symlink destination without writing through it', () => {
    const r = rig();
    const outside = path.join(directory, 'outside'); mkdirSync(outside);
    symlinkSync(outside, path.join(directory, 'Collimate'));
    expect(r.session.start(new Item(), r.contents).prevented).toBe(true);
    expect(readdirSync(outside)).toEqual([]);
  });

  it('replacing an owner cancels its transfer without delivering into the replacement', () => {
    const r = rig();
    const item = new Item(); r.session.start(item, r.contents);
    const nextEvents: unknown[] = [];
    r.policy.register(r.session, r.contents, (event) => nextEvents.push(event));
    expect(item.cancelled).toBe(true);
    expect(existsSync(item.destination)).toBe(false);
    item.emit('done', {}, 'completed');
    expect(nextEvents).toEqual([]);
    expect(r.events).toHaveLength(1);
    r.dispose(); // An old disposer cannot unregister the replacement.
    r.session.start(new Item(), r.contents);
    expect(nextEvents).toHaveLength(1);
  });

  it('cancels incomplete owned bytes and suppresses stale events after owner replacement', () => {
    const r = rig();
    const item = new Item(); r.session.start(item, r.contents);
    r.dispose();
    expect(item.cancelled).toBe(true);
    expect(existsSync(item.destination)).toBe(false);
    const replacementEvents: unknown[] = [];
    r.policy.register(r.session, r.contents, (event) => replacementEvents.push(event));
    item.emit('done', {}, 'completed');
    expect(replacementEvents).toEqual([]);
    expect(r.events).toHaveLength(1);
  });
});
