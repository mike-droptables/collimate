import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { getTheme } from '@/lib/themes';
import { MARK_LENS, MARK_RAYS, MARK_TIP } from '@/lib/brandMark';

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const read = (name: string) => readFileSync(path.join(FRONTEND, name), 'utf8');
const night = getTheme('night').vars;

describe('the favicon and text selection paint in the skin (X01)', () => {
    it('the favicon is the top bar mark in night brass on night canvas, and nothing else (X01)', () => {
        const svg = read('public/favicon.svg');
        const colours = [...svg.matchAll(/#[0-9a-f]{3,8}\b/gi)].map(m => m[0].toLowerCase());
        expect(new Set(colours)).toEqual(new Set([night.canvas, night.brass]));
        expect(svg).toMatch(new RegExp(`<rect[^>]*fill="${night.canvas}"`, 'i'));
        // Three strokes from the left edge (the mock's i-mark), each cut at
        // the lens, so three sub-paths; the tip is a separate filled path.
        const mark = svg.match(/<path[^>]*d="([^"]+)"[^>]*stroke="([^"]+)"/i);
        expect(mark?.[2].toLowerCase()).toBe(night.brass);
        expect(mark?.[1].match(/M/g)).toHaveLength(3);
        const tip = svg.match(/<path[^>]*d="([^"]+)"[^>]*fill="([^"]+)"\/>/i);
        expect(tip?.[2].toLowerCase()).toBe(night.brass);
    });

    it('the favicon carries the top bar mark\'s lens, scaled into its 64 box (polish)', () => {
        const svg = read('public/favicon.svg');
        // The 24 box maps to the favicon's 64 box at x1.75 + 11.
        const at = (n: number) => n * 1.75 + 11;
        const ring = svg.match(/<circle[^>]*cx="([^"]+)"[^>]*cy="([^"]+)"[^>]*r="([^"]+)"[^>]*stroke="([^"]+)"[^>]*stroke-width="([^"]+)"/i);
        expect(ring).not.toBeNull();
        const [cx, cy, r] = ring!.slice(1, 4).map(Number);
        expect([cx, cy]).toEqual([at(MARK_LENS.cx), at(MARK_LENS.cy)]);
        expect(r).toBeCloseTo(MARK_LENS.r * 1.75, 6);
        expect(ring![4].toLowerCase()).toBe(night.brass);
        const stroke = Number(ring![5]);
        expect(svg).toMatch(new RegExp(`<path[^>]*stroke-width="${stroke}"`));

        const pieces = (d: string) => d.split('M').filter(Boolean).map(p => p.trim().split(/\s+/).map(Number));
        const glyph = pieces(MARK_RAYS).map(seg => seg.map(at));
        const favicon = pieces(svg.match(/<path[^>]*d="([^"]+)"/i)![1]);
        expect(favicon).toHaveLength(glyph.length);
        favicon.forEach(([x1, y1, x2, y2], i) => {
            // Each piece lies on the glyph's scaled ray and keeps its left
            // edge; only the cut at the lens may move, since the favicon's
            // heavier stroke needs a wider cut.
            const [gx1, gy1, gx2, gy2] = glyph[i];
            expect(x1).toBeCloseTo(gx1, 2);
            expect(y1).toBeCloseTo(gy1, 2);
            const cross = (gx2 - gx1) * (y2 - y1) - (gy2 - gy1) * (x2 - x1);
            expect(Math.abs(cross) / Math.hypot(gx2 - gx1, gy2 - gy1) / Math.hypot(x2 - x1, y2 - y1)).toBeLessThan(0.002);
            // The ray's stroke stays clear of the ring's stroke.
            expect(Math.hypot(x2 - cx, y2 - cy)).toBeGreaterThan(r + stroke + 1);
        });
        // The tip is the glyph's filled triangle, scaled the same way.
        const tipPath = svg.match(/<path[^>]*d="([^"]+)"[^>]*fill="#/i)![1];
        const tip = tipPath.replace(/^M|Z$/g, '').trim().split(/\s+/).map(Number);
        const glyphTip = MARK_TIP.replace(/^M|Z$/g, '').trim().split(/\s+/).map(Number).map(at);
        tip.forEach((v, i) => expect(v).toBeCloseTo(glyphTip[i], 2));
    });

    it('globals.css selects text on --brass-soft with --ink, and inside code on --teal-soft (X01)', () => {
        const css = read('src/app/globals.css');
        const rule = (selector: RegExp) => css.match(new RegExp(`${selector.source}\\s*\\{([^}]*)\\}`))?.[1] ?? '';
        const base = rule(/\n\s*::selection/);
        expect(base).toMatch(/background:\s*var\(--brass-soft\)/);
        expect(base).toMatch(/color:\s*var\(--ink\)/);
        expect(rule(/code ::selection,[^{]*/)).toMatch(/background:\s*var\(--teal-soft\)/);
    });
});
