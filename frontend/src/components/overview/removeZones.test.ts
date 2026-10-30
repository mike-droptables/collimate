import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
    REMOVE_ZONES_MAX, addZone, applyZoneAction, deleteZone, effectiveZones, moveZone, normalizeWidths, removeBodyOf,
    removeZoneAt, resizeZones, settledZones, zoneEdges, zoneLabel, type RemoveZone,
} from './removeZones';

const here = path.dirname(fileURLToPath(import.meta.url));

const zone = (id: string, width: number, action: RemoveZone['action'] = { kind: 'remove' }): RemoveZone => ({ id, width, action });
// Three zones: + done (25%), plain remove (50%), − wip (25%).
const THREE = [zone('a', 0.25, { kind: 'add_tag', tag: 'done' }), zone('b', 0.5), zone('c', 0.25, { kind: 'remove_tag', tag: 'wip' })];
const strip = { left: 100, right: 500 };

describe('remove zones (TG-06)', () => {
    it('no zones is one full-width plain remove (TG-06)', () => {
        expect(effectiveZones([])).toEqual([{ id: 'remove', width: 1, action: { kind: 'remove' } }]);
        expect(effectiveZones(THREE)).toEqual(THREE);
        expect(zoneEdges([])).toEqual([0, 1]);
    });

    it('the edges are the cumulative shares, read from the widths\' sum (TG-06)', () => {
        expect(zoneEdges(THREE)).toEqual([0, 0.25, 0.75, 1]);
        expect(zoneEdges([zone('a', 2), zone('b', 2)])).toEqual([0, 0.5, 1]);
    });

    it('removeZoneAt finds the zone under x, an edge belonging to the zone it starts, and clamps past either end (TG-06)', () => {
        expect(removeZoneAt(150, strip, THREE)).toBe(0);
        expect(removeZoneAt(199, strip, THREE)).toBe(0);
        expect(removeZoneAt(200, strip, THREE)).toBe(1);
        expect(removeZoneAt(399, strip, THREE)).toBe(1);
        expect(removeZoneAt(400, strip, THREE)).toBe(2);
        expect(removeZoneAt(500, strip, THREE)).toBe(2);
        expect(removeZoneAt(20, strip, THREE)).toBe(0);
        expect(removeZoneAt(900, strip, THREE)).toBe(2);
        expect(removeZoneAt(300, strip, [])).toBe(0);
        expect(removeZoneAt(300, { left: 10, right: 10 }, THREE)).toBe(0);
    });

    it('labels a zone "+ tag", "− tag" or "Standard", the plain remove (TG-06)', () => {
        expect(THREE.map(z => zoneLabel(z.action))).toEqual(['+ done', 'Standard', '\u2212 wip']);
    });

    it('a divider moves between its neighbours, never closer than the minimum (TG-06)', () => {
        expect(resizeZones(THREE, 0, 0.4).map(z => z.width)).toEqual([0.4, 0.35, 0.25]);
        // Clamped 8% clear of the left end and of the next divider.
        expect(resizeZones(THREE, 0, 0.01).map(z => z.width)).toEqual([0.08, 0.67, 0.25]);
        expect(resizeZones(THREE, 0, 0.9).map(z => z.width)).toEqual([0.67, 0.08, 0.25]);
        expect(resizeZones(THREE, 1, 0.99).map(z => z.width)).toEqual([0.25, 0.67, 0.08]);
        expect(resizeZones(THREE, 0, 0.5, 0.2).map(z => z.width)).toEqual([0.5, 0.25, 0.25]);
        // A divider that is not there changes nothing.
        expect(resizeZones(THREE, 2, 0.5)).toEqual(THREE);
        expect(resizeZones(THREE, -1, 0.5)).toEqual(THREE);
    });

    it('moves a zone to another place, clamped to the list (TG-06)', () => {
        expect(moveZone(THREE, 0, 2).map(z => z.id)).toEqual(['b', 'c', 'a']);
        expect(moveZone(THREE, 2, 0).map(z => z.id)).toEqual(['c', 'a', 'b']);
        expect(moveZone(THREE, 1, 9).map(z => z.id)).toEqual(['a', 'c', 'b']);
        expect(moveZone(THREE, 5, 0)).toEqual(THREE);
    });

    it('adds a zone sharing the width evenly, up to the cap, and deletes one giving its width away (TG-06)', () => {
        const two = addZone([]);
        expect(two.map(z => z.width)).toEqual([0.5, 0.5]);
        expect(two.map(z => z.action)).toEqual([{ kind: 'remove' }, { kind: 'remove' }]);
        let many = two;
        while (many.length < REMOVE_ZONES_MAX) many = addZone(many);
        expect(addZone(many)).toHaveLength(REMOVE_ZONES_MAX);
        expect(many.reduce((sum, z) => sum + z.width, 0)).toBeCloseTo(1, 6);
        expect(new Set(many.map(z => z.id)).size).toBe(REMOVE_ZONES_MAX);
        expect(deleteZone(THREE, 1).map(z => [z.id, z.width])).toEqual([['a', 0.5], ['c', 0.5]]);
        expect(normalizeWidths([zone('a', 1), zone('b', 2)]).map(z => z.width)).toEqual([0.3333, 0.6667]);
    });

    it('applies a zone\'s action to a cell\'s tags, as the server does, and sends it as the remove body (TG-06)', () => {
        expect(applyZoneAction(['wip'], { kind: 'add_tag', tag: '  Done ' })).toEqual(['wip', 'Done']);
        expect(applyZoneAction(['done'], { kind: 'add_tag', tag: 'DONE' })).toEqual(['done']);
        expect(applyZoneAction(['WIP', 'x'], { kind: 'remove_tag', tag: 'wip' })).toEqual(['x']);
        expect(applyZoneAction(['x'], { kind: 'remove' })).toEqual(['x']);
        expect(applyZoneAction(['x'], { kind: 'add_tag', tag: '   ' })).toEqual(['x']);
        expect(applyZoneAction(['x'])).toEqual(['x']);
        expect(removeBodyOf({ kind: 'add_tag', tag: ' done ' })).toEqual({ add_tags: ['done'] });
        expect(removeBodyOf({ kind: 'remove_tag', tag: 'wip' })).toEqual({ remove_tags: ['wip'] });
        expect(removeBodyOf({ kind: 'remove' })).toBeUndefined();
        expect(removeBodyOf({ kind: 'add_tag', tag: '' })).toBeUndefined();
        expect(removeBodyOf()).toBeUndefined();
    });

    it('the zone cap is the server\'s (TG-06)', () => {
        const source = readFileSync(path.resolve(here, '../../../../api/channel_settings.py'), 'utf8');
        expect(Number(source.match(/^REMOVE_ZONES_MAX\s*(?::\s*int\s*)?=\s*(\d+)/m)?.[1])).toBe(REMOVE_ZONES_MAX);
    });

    it('settles the zones as the server keeps them: a tag zone with no tag goes, the rest share the width (TG-06)', () => {
        expect(settledZones(THREE)).toEqual(THREE);
        const blank = [zone('a', 0.25, { kind: 'add_tag', tag: '  ' }), zone('b', 0.5), zone('c', 0.25, { kind: 'remove_tag', tag: 'wip' })];
        expect(settledZones(blank).map(z => [z.id, z.width])).toEqual([['b', 0.6667], ['c', 0.3333]]);
        expect(settledZones([zone('a', 1, { kind: 'add_tag', tag: '' })])).toEqual([]);
        expect(settledZones([])).toEqual([]);
    });
});
