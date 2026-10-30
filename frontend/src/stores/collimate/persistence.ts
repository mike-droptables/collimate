// localStorage persistence helpers for useCollimateStore.
//
// Kept in a sibling file so the store itself stays focused on state
// transitions; the load*/save* pairs here are pure, test-friendly, and
// share the same keyspace contract across the app.
//
// Every save is wrapped in try/catch — SSR (no window), quota-full, and
// privacy-mode storage all surface as thrown DOMExceptions. The store
// tolerates "nothing persisted" fine; losing a write is always better
// than tanking a state mutation.

import type {LastFinishedRun} from './types';

// ── keys ─────────────────────────────────────────────────────────────
export const LS_LAST_FINISHED_KEY = 'collimate.lastFinishedByCell.v1';

/** Write a per-cell record, keeping only the `cap` most recently touched
 *  entries (by `at`) once it grows past the cap. */
function saveCapped<T extends { at?: number }>(key: string, r: Record<string, T>, cap: number): void {
    try {
        let entries = Object.entries(r);
        if (entries.length > cap) {
            entries = entries
                .sort((a, b) => (b[1].at || 0) - (a[1].at || 0))
                .slice(0, cap);
        }
        localStorage.setItem(key, JSON.stringify(Object.fromEntries(entries)));
    } catch { /* quota / SSR */ }
}

// ── last finished run per cell ───────────────────────────────────────
// A failed run's red light must survive a reload until the user has seen
// it; capped at LAST_FINISHED_CAP, oldest finish first out.

export const LAST_FINISHED_CAP = 500;

export function loadLastFinished(): Record<string, LastFinishedRun> {
    try {
        const raw = localStorage.getItem(LS_LAST_FINISHED_KEY);
        if (!raw) return {};
        const obj = JSON.parse(raw);
        if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return {};
        const out: Record<string, LastFinishedRun> = {};
        for (const [cellId, v] of Object.entries(obj)) {
            if (!v || typeof v !== 'object') continue;
            const e = v as Record<string, unknown>;
            if (typeof e.runId !== 'string' || typeof e.label !== 'string'
                || typeof e.status !== 'string' || typeof e.seen !== 'boolean') continue;
            out[cellId] = {
                runId: e.runId,
                label: e.label,
                status: e.status,
                seen: e.seen,
                at: typeof e.at === 'number' ? e.at : 0,
                ...(typeof e.reason === 'string' ? { reason: e.reason } : {}),
            };
        }
        return out;
    } catch { return {}; }
}

export function saveLastFinished(r: Record<string, LastFinishedRun>) {
    saveCapped(LS_LAST_FINISHED_KEY, r, LAST_FINISHED_CAP);
}
