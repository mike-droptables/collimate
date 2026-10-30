import type { RemoveTagAction } from '@/lib/api/cells';
import type { RemoveZone, ZoneAction } from '@/lib/api/settings';
import { foldTag, hasTag, normalizeTag } from '@/lib/tags';

export type { RemoveZone, ZoneAction };

// The remove zones (pure): the Removed bar and the shelf's drop area split
// left to right into the channel's zones (`remove_zones` in the settings
// document), each a share of the width and an action. A board card let go
// over a zone is removed with that zone's action: a plain remove, or one
// that adds or takes away a tag on the removed record. No zones is one
// full-width plain remove. The bar, the shelf and the drop all read one
// strip's rect (`[data-rm-zone-strip]`), so what shows and what the drop
// resolves cannot drift apart.

/** The server keeps at most this many zones (channel_settings.REMOVE_ZONES_MAX). */
export const REMOVE_ZONES_MAX = 6;
/** A divider stops this close (a share of the width) to its neighbours. */
export const ZONE_MIN_FRACTION = 0.08;

const round4 = (n: number) => Math.round(n * 1e4) / 1e4;

let zoneSeq = 0;
/** A fresh zone id (unique in this window; the server keeps it as given). */
export const newZoneId = (): string => `z${Date.now().toString(36)}${(zoneSeq++).toString(36)}`;

/** The zones a drop uses: the configured ones, or one full-width plain
 *  remove when there are none. */
export function effectiveZones(zones: readonly RemoveZone[]): RemoveZone[] {
    return zones.length > 0 ? [...zones] : [{ id: 'remove', width: 1, action: { kind: 'remove' } }];
}

/** The zones' edges as shares of the width, left to right: n + 1 values
 *  from 0 to 1 (widths read as shares of their sum). */
export function zoneEdges(zones: readonly RemoveZone[]): number[] {
    const list = effectiveZones(zones);
    const total = list.reduce((sum, zone) => sum + Math.max(0, zone.width), 0);
    const edges = [0];
    let at = 0;
    for (const zone of list) {
        at += total > 0 ? Math.max(0, zone.width) / total : 1 / list.length;
        edges.push(at);
    }
    edges[edges.length - 1] = 1;
    return edges;
}

/** The zone under window x `clientX` on a strip spanning `rect`: past
 *  either end, the end zone. */
export function removeZoneAt(clientX: number, rect: { left: number; right: number }, zones: readonly RemoveZone[]): number {
    const edges = zoneEdges(zones);
    const last = edges.length - 2;
    const span = rect.right - rect.left;
    if (span <= 0) return 0;
    const f = (clientX - rect.left) / span;
    for (let i = 0; i < last; i++) {
        if (f < edges[i + 1]) return i;
    }
    return last;
}

/** A zone's label: "+ tag", "− tag" or "Standard" (a plain remove). */
export function zoneLabel(action: ZoneAction): string {
    if (action.kind === 'add_tag') return `+ ${action.tag}`;
    if (action.kind === 'remove_tag') return `− ${action.tag}`;
    return 'Standard';
}

/** The widths as shares summing to 1 (rounded to 1e-4, the last zone
 *  taking the remainder). */
export function normalizeWidths(zones: readonly RemoveZone[]): RemoveZone[] {
    const edges = zoneEdges(zones);
    const list = effectiveZones(zones);
    let used = 0;
    return list.map((zone, i) => {
        const width = i === list.length - 1 ? round4(1 - used) : round4(edges[i + 1] - edges[i]);
        used += width;
        return { ...zone, width };
    });
}

/** Move divider `dividerIdx` (between zone i and zone i + 1) to `fraction`
 *  of the width, kept `minFraction` clear of the dividers either side. */
export function resizeZones(
    zones: readonly RemoveZone[], dividerIdx: number, fraction: number, minFraction = ZONE_MIN_FRACTION,
): RemoveZone[] {
    const list = normalizeWidths(zones);
    if (dividerIdx < 0 || dividerIdx >= list.length - 1) return list;
    const edges = zoneEdges(list);
    const lo = edges[dividerIdx] + minFraction;
    const hi = edges[dividerIdx + 2] - minFraction;
    if (hi < lo) return list;
    const at = Math.min(hi, Math.max(lo, fraction));
    const pair = list[dividerIdx].width + list[dividerIdx + 1].width;
    const left = round4(at - edges[dividerIdx]);
    const next = [...list];
    next[dividerIdx] = { ...list[dividerIdx], width: left };
    next[dividerIdx + 1] = { ...list[dividerIdx + 1], width: round4(pair - left) };
    return next;
}

/** Zone `from` moved to index `to` (clamped to the list). */
export function moveZone(zones: readonly RemoveZone[], from: number, to: number): RemoveZone[] {
    const list = [...zones];
    if (from < 0 || from >= list.length) return list;
    const target = Math.max(0, Math.min(list.length - 1, to));
    const [zone] = list.splice(from, 1);
    list.splice(target, 0, zone);
    return list;
}

/** One more zone at the right end, the zones sharing the width evenly
 *  (no more than REMOVE_ZONES_MAX). */
export function addZone(zones: readonly RemoveZone[], action: ZoneAction = { kind: 'remove' }): RemoveZone[] {
    const list = effectiveZones(zones);
    if (list.length >= REMOVE_ZONES_MAX) return list;
    const share = 1 / (list.length + 1);
    return normalizeWidths([...list.map(zone => ({ ...zone, width: share })), { id: newZoneId(), width: share, action }]);
}

/** Zone `index` gone, its width given to the zones that stay. */
export function deleteZone(zones: readonly RemoveZone[], index: number): RemoveZone[] {
    const list = effectiveZones(zones);
    if (index < 0 || index >= list.length) return list;
    const rest = list.filter((_, i) => i !== index);
    return rest.length > 0 ? normalizeWidths(rest) : [];
}

/** A cell's tags after a zone's action (the server's apply_tag_action):
 *  the tag added (once, by case-folded spelling) or taken away. A plain
 *  remove, or a tag that normalises to nothing, leaves them as they are. */
export function applyZoneAction(tags: readonly string[], action?: ZoneAction | null): string[] {
    const tag = action && action.kind !== 'remove' ? normalizeTag(action.tag) : null;
    if (!action || !tag) return [...tags];
    if (action.kind === 'add_tag') return hasTag(tags, tag) ? [...tags] : [...tags, tag];
    return tags.filter(t => foldTag(t) !== foldTag(tag));
}

/** The remove route's body for a zone's action: none for a plain remove. */
export function removeBodyOf(action?: ZoneAction | null): RemoveTagAction | undefined {
    if (!action || action.kind === 'remove') return undefined;
    const tag = normalizeTag(action.tag);
    if (!tag) return undefined;
    return action.kind === 'add_tag' ? { add_tags: [tag] } : { remove_tags: [tag] };
}

/** A tag zone whose tag normalises to nothing: the server drops it. */
export const unsettledZone = (zone: RemoveZone): boolean =>
    zone.action.kind !== 'remove' && normalizeTag(zone.action.tag) === null;

/** The zones as the server keeps them: a tag zone left without a tag gone,
 *  the widths of the rest renormalised (none left: no zones). */
export function settledZones(zones: readonly RemoveZone[]): RemoveZone[] {
    if (!zones.some(unsettledZone)) return [...zones];
    const rest = zones.filter(zone => !unsettledZone(zone));
    return rest.length > 0 ? normalizeWidths(rest) : [];
}
