import type { CellDisplay } from './cellDisplay';
import { requestJson } from './api/client';
export const fetchCellDisplays = (channel: string): Promise<Record<string, CellDisplay>> => requestJson(`/channels/${encodeURIComponent(channel)}/cell_display`, 'GET');
const put = (channel: string, cell: string, display: CellDisplay) => requestJson(`/channels/${encodeURIComponent(channel)}/cell_display/${encodeURIComponent(cell)}`, 'PUT', display);
interface Entry {
    channel: string;
    cell: string;
    pending?: CellDisplay;
    running?: Promise<void>;
    timer?: ReturnType<typeof setTimeout>;
    error?: Error;
    notify: (error: string | null) => void;
}
/** One immutable channel/cell queue: one request in flight and the latest unstarted view. */
export class CellDisplayWrites {
    private entries = new Map<string, Entry>();
    constructor(private write = put, private delay = 350) { }
    private key(channel: string, cell: string) { return JSON.stringify([channel, cell]); }
    schedule(channel: string, cell: string, display: CellDisplay, notify: (error: string | null) => void) {
        const key = this.key(channel, cell);
        const entry = this.entries.get(key) ?? { channel, cell, notify };
        entry.pending = display;
        entry.notify = notify;
        if (entry.timer)
            clearTimeout(entry.timer);
        entry.timer = setTimeout(() => { entry.timer = undefined; void this.drain(entry).catch(() => undefined); }, this.delay);
        this.entries.set(key, entry);
    }
    private drain(entry: Entry): Promise<void> {
        if (entry.running)
            return entry.running;
        entry.running = (async () => {
            while (entry.pending) {
                const display = entry.pending;
                entry.pending = undefined;
                try {
                    await this.write(entry.channel, entry.cell, display);
                    entry.error = undefined;
                    entry.notify(null);
                }
                catch (error) {
                    entry.pending ??= display;
                    entry.error = error instanceof Error ? error : new Error(String(error));
                    entry.notify(entry.error.message);
                    throw entry.error;
                }
            }
        })().finally(() => { entry.running = undefined; if (!entry.pending) {
            if (entry.timer)
                clearTimeout(entry.timer);
            this.entries.delete(this.key(entry.channel, entry.cell));
        } });
        return entry.running;
    }
    async flush(channel: string, cells: readonly string[]) {
        await Promise.all(cells.map(async (cell) => {
            const entry = this.entries.get(this.key(channel, cell));
            if (!entry)
                return;
            if (entry.timer) {
                clearTimeout(entry.timer);
                entry.timer = undefined;
            }
            do {
                await this.drain(entry);
            } while (entry.pending);
        }));
    }
    forget(channel: string, cells: readonly string[]) {
        for (const cell of cells) {
            const key = this.key(channel, cell), entry = this.entries.get(key);
            if (entry?.timer)
                clearTimeout(entry.timer);
            this.entries.delete(key);
        }
    }
}
export const cellDisplayWrites = new CellDisplayWrites();
export const flushCellDisplays = (channel: string, cells: readonly string[]) => cellDisplayWrites.flush(channel, cells);
