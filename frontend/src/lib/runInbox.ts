// A live run's inbox for approval cards (hooks/useRunInbox): the pure half
// of the protocol. The run serves one broadcast, transport-only chat session
// (GET /runs/{id}/inbox, the /api/chats/{id} WebSocket of
// api/routes/chat_live.py). Its frames carry the run's streamed text too;
// only three matter here: a history frame's `pending_requests`, a
// `permission_request` and a `permission_resolved`. Everything else (a
// replayed text delta, a turn bracket) leaves the cards as they are.
// Ported from the SDK assistant renderer's message-model.

import { getApiUrl } from '@/lib/api/client';

/** One approval card (gen.ask), open until it is answered or resolved. */
export interface PendingPermission {
    id: string;
    title: string;
    kind: string;
    detail?: string;
    options: { option_id: string; name: string; kind: string }[];
}

/** While the run has registered no inbox yet, ask again this often… */
export const INBOX_RETRY_MS = 2000;
/** …this many times before giving up. */
export const INBOX_RETRY_LIMIT = 60;
/** The socket's close codes for a session that is gone or a bad token. */
export const INBOX_CLOSE_GONE = 4404;
export const INBOX_CLOSE_UNAUTHORIZED = 4401;

/** A card from a `permission_request` frame or a history frame's
 *  `pending_requests` entry; null without an id. */
export function normalizePermission(r: unknown): PendingPermission | null {
    if (!r || typeof r !== 'object') return null;
    const v = r as Record<string, unknown>;
    if (!v.id) return null;
    const options = Array.isArray(v.options)
        ? v.options
            .filter((o): o is Record<string, unknown> => !!o && typeof o === 'object' && !!(o as Record<string, unknown>).option_id)
            .map(o => ({ option_id: String(o.option_id), name: String(o.name || ''), kind: String(o.kind || '') }))
        : [];
    return {
        id: String(v.id),
        title: String(v.title || 'Permission'),
        kind: String(v.kind || ''),
        ...(v.detail ? { detail: String(v.detail) } : {}),
        options,
    };
}

/** The cards after one frame. A frame that changes no card returns the
 *  SAME array. */
export function applyInboxFrame(cards: readonly PendingPermission[], frame: unknown): PendingPermission[] {
    const same = cards as PendingPermission[];
    if (!frame || typeof frame !== 'object') return same;
    const f = frame as Record<string, unknown>;
    if (f.t === 'history') {
        if (!Array.isArray(f.pending_requests)) return same;
        return f.pending_requests.map(normalizePermission).filter((c): c is PendingPermission => c !== null);
    }
    if (f.t === 'permission_request') {
        const card = normalizePermission(f);
        if (!card) return same;
        const at = cards.findIndex(c => c.id === card.id);
        return at < 0 ? [...cards, card] : cards.map((c, i) => (i === at ? card : c));
    }
    if (f.t === 'permission_resolved') {
        if (!cards.some(c => c.id === String(f.id))) return same;
        return cards.filter(c => c.id !== String(f.id));
    }
    return same;
}

/** The answer to a card: an option, or the cancel (null). */
export function answerFrame(id: string, optionId: string | null):
    { t: 'permission_answer'; id: string; option_id: string } | { t: 'permission_answer'; id: string; cancelled: true } {
    return optionId === null ? { t: 'permission_answer', id, cancelled: true } : { t: 'permission_answer', id, option_id: optionId };
}

/** The inbox socket's URL: the API's origin in ws(s), its path, the token. */
export function inboxSocketUrl(wsPath: string, token: string, apiBase: string = getApiUrl(), here: string = location.href): string {
    const origin = new URL(apiBase, here);
    const scheme = origin.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${scheme}//${origin.host}${wsPath}?token=${encodeURIComponent(token)}`;
}

/** The wait before redialling after the `attempt`-th transient close. */
export function inboxRedialMs(attempt: number): number {
    return Math.min(500 * attempt, 5000);
}
