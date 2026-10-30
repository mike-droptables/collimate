import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex } from '@noble/hashes/utils.js';
import { fileFetchCache } from '@/lib/fileFetchCache';
import type { FileBody, VirtualFile } from '@/lib/types';

/** Accepted metadata and its separate immutable body, without an eager Cell payload. */
export function fileFixture(name: string, content = '', metadata: Partial<VirtualFile> = {}): VirtualFile {
    const body_hash = bytesToHex(sha256(new TextEncoder().encode(content)));
    const file: VirtualFile = { name, language: 'plaintext', file_revision: 1, body_hash, encoding: 'utf-8', writer_id: 'fixture-writer', edit_seq: 1, ...metadata };
    if (file.encoding === 'utf-8' && file.body_hash === body_hash) fileFetchCache.put(`body:${body_hash}`, content);
    return file;
}
export function bodyFixture(name: string, content = '', metadata: Partial<VirtualFile> = {}): FileBody {
    return { ...fileFixture(name, content, metadata), content };
}
export function fixtureBodyHash(content: string): string {
    const hash = bytesToHex(sha256(new TextEncoder().encode(content)));
    fileFetchCache.put(`body:${hash}`, content);
    return hash;
}
/** Inspect explicit authoring input or the immutable fixture body independently of Cell metadata. */
export function fixtureText(file: VirtualFile | undefined): string {
    if (!file) throw new Error('Missing fixture file.');
    if ('content' in file) return String(file.content);
    const body = file.body_hash ? fileFetchCache.get<string>(`body:${file.body_hash}`) : undefined;
    if (body === undefined) throw new Error(`Missing immutable fixture body: ${file.name}`);
    return body;
}
