import { describe, expect, it } from 'vitest';
import { zipMembers } from './zipWriter';

/** The archive's members as a reader finds them: its central directory,
 *  each entry's local header, name, CRC and bytes. */
async function readZip(blob: Blob) {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    const view = new DataView(bytes.buffer);
    const end = bytes.length - 22;
    expect(view.getUint32(end, true)).toBe(0x06054b50);
    const count = view.getUint16(end + 10, true);
    let at = view.getUint32(end + 16, true);
    const members: { name: string; crc: number; flags: number; external: number; data: string }[] = [];
    for (let i = 0; i < count; i++) {
        expect(view.getUint32(at, true)).toBe(0x02014b50);
        const flags = view.getUint16(at + 8, true);
        const crc = view.getUint32(at + 16, true);
        const size = view.getUint32(at + 20, true);
        const nameLength = view.getUint16(at + 28, true);
        const external = view.getUint32(at + 38, true);
        const offset = view.getUint32(at + 42, true);
        const name = new TextDecoder().decode(bytes.slice(at + 46, at + 46 + nameLength));
        expect(view.getUint32(offset, true)).toBe(0x04034b50);
        expect(view.getUint32(offset + 14, true)).toBe(crc);
        const localName = view.getUint16(offset + 26, true) + view.getUint16(offset + 28, true);
        const data = new TextDecoder().decode(bytes.slice(offset + 30 + localName, offset + 30 + localName + size));
        members.push({ name, crc, flags, external, data });
        at += 46 + nameLength;
    }
    return members;
}

// A repository's .git uploads as a plain stored zip (zipWriter): the server
// reads it with Python's zipfile like any other upload.
describe('zipMembers', () => {
    it('stores each member with its CRC-32 and UTF-8 name, and empty folders as directory members', async () => {
        const zip = await zipMembers([
            { path: 'proj/.git/HEAD', body: new Blob(['ref: refs/heads/main\n']) },
            { path: 'proj/.git/check', body: new Blob(['123456789']) },
            { path: 'proj/.git/日本', body: new Blob([]) },
        ], ['proj/.git/refs/tags']);
        const members = await readZip(zip);
        expect(members.map(m => m.name)).toEqual(['proj/.git/HEAD', 'proj/.git/check', 'proj/.git/日本', 'proj/.git/refs/tags/']);
        expect(members[0].data).toBe('ref: refs/heads/main\n');
        expect(members[1].crc).toBe(0xcbf43926);
        expect(members[2].crc).toBe(0);
        expect(members.every(m => m.flags === 0x0800)).toBe(true);
        expect(members[3].external).toBe(0x10);
        expect(zip.type).toBe('application/zip');
    });

    it('an empty archive is just its end record', async () => {
        expect((await zipMembers([])).size).toBe(22);
    });

    it('a cancelled zip stops', async () => {
        const controller = new AbortController();
        controller.abort();
        await expect(zipMembers([{ path: 'a', body: new Blob(['x']) }], [], controller.signal)).rejects.toThrow('cancelled');
    });
});
