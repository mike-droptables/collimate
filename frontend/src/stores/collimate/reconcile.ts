import type { StatePatch, StatePathPart } from '@/lib/types';
import { AppState, Channel, Stack, Cell, ScopedAppState } from '@/lib/types';

/**
 * Structural sharing for incoming server state.
 *
 * A complete server snapshot arrives as a freshly-parsed JSON tree: new object
 * identity for every channel, stack, and cell even when nothing changed.
 * Downstream memoisation (StackGroup / StackItem comparators, StackScene's
 * per-stack callback map, StackApp's `mainStacks` memo) keys on those
 * identities, so without reconciliation a single run-progress tick re-rendered
 * the entire scene.
 *
 * These helpers walk the incoming tree against the previous one and return
 * the PREVIOUS object wherever the subtree is unchanged, so `===` checks keep
 * holding across pushes. Matching is by id (not index), so reorders still
 * reuse the moved elements' identities.
 *
 * Cell equality leans on the same invariant the render-layer comparators
 * already trust: every server-side cell mutation bumps `updated_at`.
 */

function reconcileCell(prev: Cell | undefined, next: Cell): Cell {
    if (!prev) return next;
    // `updated_at` must be present on both sides — a cell without it can't
    // prove it's unchanged, so take the fresh object.
    if (prev.updated_at !== undefined && prev.updated_at === next.updated_at) return prev;
    return next;
}

/** Shallow-equal on every own key except the listed tree keys (which are
 *  reconciled separately, element-wise). */
function shallowEqualExcept(a: object, b: object, skip: ReadonlySet<string>): boolean {
    const aKeys = Object.keys(a).filter(k => !skip.has(k));
    const bKeys = Object.keys(b).filter(k => !skip.has(k));
    if (aKeys.length !== bKeys.length) return false;
    for (const k of aKeys) {
        if ((a as Record<string, unknown>)[k] !== (b as Record<string, unknown>)[k]) return false;
    }
    return true;
}

function reconcileList<T extends { id: string }>(
    prevList: T[],
    nextList: T[],
    reconcileItem: (prev: T | undefined, next: T) => T,
): T[] {
    const prevById = new Map(prevList.map(item => [item.id, item]));
    const merged = nextList.map(item => reconcileItem(prevById.get(item.id), item));
    // Reuse the previous ARRAY identity only when it's element-for-element
    // identical (same members, same order).
    if (merged.length === prevList.length && merged.every((item, i) => item === prevList[i])) {
        return prevList;
    }
    return merged;
}

const STACK_TREE_KEYS = new Set(['cells']);

export function reconcileStack(prev: Stack | undefined, next: Stack): Stack {
    if (!prev) return next;
    const cells = reconcileList(prev.cells, next.cells, reconcileCell);
    if (cells === prev.cells && shallowEqualExcept(prev, next, STACK_TREE_KEYS)) return prev;
    return { ...next, cells };
}

const CHANNEL_TREE_KEYS = new Set(['main_stacks', 'nav_stacks']);

export function reconcileChannel(prev: Channel | undefined, next: Channel): Channel {
    if (!prev) return next;
    const main_stacks = reconcileList(prev.main_stacks, next.main_stacks, reconcileStack);
    const nav_stacks = reconcileList(prev.nav_stacks, next.nav_stacks, reconcileStack);
    if (
        main_stacks === prev.main_stacks &&
        nav_stacks === prev.nav_stacks &&
        shallowEqualExcept(prev, next, CHANNEL_TREE_KEYS)
    ) {
        return prev;
    }
    return { ...next, main_stacks, nav_stacks };
}

const STATE_TREE_KEYS = new Set(['channels']);

export function reconcileAppState(prev: AppState | null, next: AppState): AppState {
    if (!prev) return next;
    const channels = reconcileList(prev.channels, next.channels, reconcileChannel);
    if (channels === prev.channels && shallowEqualExcept(prev, next, STATE_TREE_KEYS)) return prev;
    return { ...next, channels };
}


/** Normalize the scoped transport into the complete internal navigation model. */
export function mergeScopedState(previous: AppState | null, incoming: AppState | import('@/lib/types').ScopedAppState): AppState {
    const prior = new Map(previous?.channels.map(channel => [channel.id, channel]) ?? []);
    return {
        active_channel_id: incoming.active_channel_id,
        channels: incoming.channels.map(channel => {
            if ('main_stacks' in channel && 'nav_stacks' in channel) return channel;
            const existing = prior.get(channel.id);
            if (existing) return existing.name === channel.name ? existing : { ...existing, name: channel.name };
            return { ...channel, main_stacks: [], nav_stacks: [], main_selection_id: null, nav_selection_id: null };
        }),
    };
}


/** Apply a revision's keyed operations without mutating its prior wire snapshot.
 * Unknown members refuse the entire patch; names are literal own-property keys.
 */
export function applyStatePatches(snapshot: ScopedAppState, patches: StatePatch[]): ScopedAppState {
    const own = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key);
    const record = (value: unknown): value is Record<string, unknown> =>
        value !== null && typeof value === 'object' && !Array.isArray(value);
    function change(node: unknown, path: StatePathPart[], patch: StatePatch): unknown {
        if (!path.length) {
            if (patch.op !== 'sort' || !Array.isArray(node)
                || node.some(item => !record(item) || !own(item, 'name') || typeof item.name !== 'string')
                || new Set(node.map(item => item.name)).size !== node.length)
                throw new Error('Invalid state file ordering');
            // Python strings sort by Unicode code point, not JS UTF-16 units.
            return node.slice().sort((a, b) => {
                const left = Array.from(a.name as string, char => char.codePointAt(0)!);
                const right = Array.from(b.name as string, char => char.codePointAt(0)!);
                for (let i = 0; i < Math.min(left.length, right.length); i++)
                    if (left[i] !== right[i]) return left[i] - right[i];
                return left.length - right.length;
            });
        }
        const [part, ...rest] = path;
        if (typeof part === 'string') {
            if (!record(node)) throw new Error('State field parent missing');
            if (rest.length) {
                if (!own(node, part)) throw new Error('State field missing');
                return { ...node, [part]: change(node[part], rest, patch) };
            }
            if (patch.op === 'sort') {
                if (part !== 'files' || !own(node, part)) throw new Error('Invalid state file ordering');
                return { ...node, [part]: change(node[part], [], patch) };
            }
            if (patch.op === 'set') return { ...node, [part]: patch.value };
            if (patch.op !== 'remove' || !own(node, part)) throw new Error('Invalid state field operation');
            const next = { ...node };
            delete next[part];
            return next;
        }
        if (!part || (part.key !== 'id' && part.key !== 'name') || typeof part.value !== 'string'
            || !Array.isArray(node)) throw new Error('Invalid state member selector');
        const index = node.findIndex(item => record(item) && own(item, part.key) && item[part.key] === part.value);
        const next = node.slice();
        if (rest.length) {
            if (index < 0) throw new Error('State member missing');
            next[index] = change(node[index], rest, patch);
        } else if (patch.op === 'insert') {
            if (index >= 0 || !Number.isInteger(patch.index) || patch.index < 0 || patch.index > node.length
                || !record(patch.value) || !own(patch.value, part.key) || patch.value[part.key] !== part.value)
                throw new Error('Invalid state insertion');
            next.splice(patch.index, 0, patch.value);
        } else {
            if (index < 0) throw new Error('State member missing');
            if (patch.op === 'remove') next.splice(index, 1);
            else if (patch.op === 'move') {
                if (!Number.isInteger(patch.index) || patch.index < 0 || patch.index >= node.length)
                    throw new Error('Invalid state move');
                next.splice(index, 1);
                next.splice(patch.index, 0, node[index]);
            } else if (patch.op === 'set') {
                if (!record(patch.value) || !own(patch.value, part.key) || patch.value[part.key] !== part.value)
                    throw new Error('Invalid state replacement');
                next[index] = patch.value;
            } else throw new Error('Invalid state operation');
        }
        return next;
    }
    let next: unknown = snapshot;
    for (const patch of patches) {
        if (!patch.path.length) throw new Error('Empty state patch path');
        next = change(next, patch.path, patch);
    }
    return next as ScopedAppState;
}
