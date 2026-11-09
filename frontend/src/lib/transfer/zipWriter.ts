// A plain zip of files the browser holds (Files and Blobs), built without
// reading them into memory twice: each member is stored (no compression),
// its CRC-32 computed from 8 MiB slices, and the archive is a Blob whose
// parts are the headers and the files themselves. Folders that hold nothing
// travel as directory members. No zip64: a member or archive past 4 GiB,
// or past 65,535 members, is refused.

/** One member: its path in the archive ('/' separators) and its bytes. */
export interface ZipMember {
    path: string;
    body: Blob;
}

const SLICE = 8 * 1024 * 1024;
const LIMIT = 0xffffffff;
const MAX_MEMBERS = 0xffff;
/** UTF-8 names (general purpose flag bit 11). */
const UTF8 = 0x0800;
/** 1980-01-01 00:00, the earliest DOS date. */
const DOS_DATE = 0x0021;

const CRC_TABLE = (() => {
    const table = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
        let c = n;
        for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
        table[n] = c >>> 0;
    }
    return table;
})();

/** The CRC-32 of `body`, read a slice at a time. */
async function crc32(body: Blob, signal?: AbortSignal): Promise<number> {
    let crc = 0xffffffff;
    for (let at = 0; at < body.size; at += SLICE) {
        if (signal?.aborted) throw new DOMException('The upload was cancelled.', 'AbortError');
        const bytes = new Uint8Array(await body.slice(at, at + SLICE).arrayBuffer());
        for (let i = 0; i < bytes.length; i++) crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
    }
    return (crc ^ 0xffffffff) >>> 0;
}

/** A little-endian header of `fields`, each [byte width, value]. */
function header(fields: readonly (readonly [2 | 4, number])[], name: Uint8Array): Uint8Array<ArrayBuffer> {
    const size = fields.reduce((n, [width]) => n + width, 0);
    const out = new Uint8Array(size + name.length);
    const view = new DataView(out.buffer);
    let at = 0;
    for (const [width, value] of fields) {
        if (width === 2) view.setUint16(at, value, true); else view.setUint32(at, value, true);
        at += width;
    }
    out.set(name, size);
    return out;
}

/** Zip `members` and the empty folders `dirs` (paths without a trailing
 *  slash). */
export async function zipMembers(members: readonly ZipMember[], dirs: readonly string[] = [], signal?: AbortSignal): Promise<Blob> {
    if (members.length + dirs.length > MAX_MEMBERS) throw new Error('Too many files to zip (more than 65,535).');
    const encoder = new TextEncoder();
    const parts: BlobPart[] = [];
    const central: Uint8Array<ArrayBuffer>[] = [];
    let offset = 0;
    const add = (path: string, body: Blob | null, crc: number) => {
        const name = encoder.encode(path);
        const size = body?.size ?? 0;
        if (size > LIMIT || offset > LIMIT) throw new Error('Too large to zip (over 4 GiB).');
        const common = [[2, 20], [2, UTF8], [2, 0], [2, 0], [2, DOS_DATE], [4, crc], [4, size], [4, size], [2, name.length]] as const;
        const local = header([[4, 0x04034b50], ...common, [2, 0]], name);
        central.push(header([[4, 0x02014b50], [2, 20], ...common, [2, 0], [2, 0], [2, 0], [2, 0], [4, body ? 0 : 0x10], [4, offset]], name));
        parts.push(local);
        if (body) parts.push(body);
        offset += local.length + size;
    };
    for (const member of members) add(member.path, member.body, await crc32(member.body, signal));
    for (const dir of dirs) add(`${dir}/`, null, 0);
    const directorySize = central.reduce((n, entry) => n + entry.length, 0);
    if (offset + directorySize > LIMIT) throw new Error('Too large to zip (over 4 GiB).');
    const count = central.length;
    const end = header([[4, 0x06054b50], [2, 0], [2, 0], [2, count], [2, count], [4, directorySize], [4, offset], [2, 0]], new Uint8Array());
    return new Blob([...parts, ...central, end], { type: 'application/zip' });
}
