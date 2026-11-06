// Archived channels on the client (plan 1105 S6, P7). An archived channel
// opens read-only: the server refuses every edit and every run on it with
// one 409 shape, `{detail: {code: 'channel_archived', message}}`. The page
// disables the controls it knows (ChannelReadOnlyContext); a refusal from a
// control it missed is reported here once, through the listener StackApp
// registers. The view state a reader changes in passing (a cell's open
// tabs, the Commands pane, the overview) stays local while the channel is
// archived: `readOnlyChannel` names the channel whose writes are skipped.
// No imports: the API client and the store both read this module.

export const CHANNEL_ARCHIVED = 'channel_archived';

/** The server's sentence when `error` is its archived-channel refusal. */
export function archivedRefusalMessage(error: unknown): string | null {
    const detail = (error as { response?: { status?: number; data?: { detail?: unknown } } })?.response;
    if (detail?.status !== 409) return null;
    const body = detail.data?.detail as { code?: unknown; message?: unknown } | undefined;
    if (!body || typeof body !== 'object' || body.code !== CHANNEL_ARCHIVED) return null;
    return typeof body.message === 'string' && body.message.trim()
        ? body.message
        : 'This channel is archived and read-only.';
}

type RefusalListener = (message: string) => void;
let listener: RefusalListener | null = null;

/** Receive every archived-channel refusal (one listener: the app's). */
export function onArchivedRefusal(next: RefusalListener): () => void {
    listener = next;
    return () => { if (listener === next) listener = null; };
}

/** Called by the API client for each refusal it sees. */
export function reportArchivedRefusal(message: string): void {
    listener?.(message);
}

let readOnly: string | null = null;

/** The active channel when it is archived (set from each state snapshot),
 *  else null. */
export function setReadOnlyChannel(channelId: string | null): void {
    readOnly = channelId;
}

/** Whether writes to `channelId` are skipped: it is the archived active
 *  channel. */
export function isReadOnlyChannel(channelId: string | null | undefined): boolean {
    return !!channelId && channelId === readOnly;
}

/** The channels a list offers: every one that is not archived. The server
 *  lists an archived channel only while it is the active one. */
export function listedChannels<T extends { id: string; archived?: boolean }>(channels: readonly T[]): T[] {
    return channels.filter(channel => !channel.archived);
}
