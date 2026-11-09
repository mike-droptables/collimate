import { describe, expect, it } from 'vitest';
import { SCENE_MAJOR, SCENE_PITCHES, type ThemeScene } from './themes';
import { sceneBackground, sceneGeometry, type ScenePaint } from './sceneLayers';

const PAINT: ScenePaint = { ink: 'INK', inkFaint: 'FAINT', glowWarm: 'WARM', glowCool: 'COOL', vignette: 'SHADE', image: 'PICTURE' };
const FLAT = { pattern: 'none', glowWarm: null, glowCool: null, vignette: 0, image: null } as const;
const g = sceneGeometry(16, 1);

/** Whole device pixels, allowing float noise. */
const whole = (cssPx: number, dpr: number) => Math.abs(cssPx * dpr - Math.round(cssPx * dpr)) < 1e-9;

describe('scene geometry', () => {
    it('every length is a whole number of device pixels at every ratio and interface scale', () => {
        const scales = [0.7, 0.8, 0.9, 1, 1.1, 1.25, 1.5];
        for (const dpr of [1, 1.25, 1.5, 1.75, 2, 3].flatMap(device => scales.map(scale => device * scale))) {
            for (const pitch of SCENE_PITCHES) {
                const geometry = sceneGeometry(pitch, dpr);
                for (const [name, length] of Object.entries(geometry)) {
                    expect(whole(length, dpr), `${pitch} at ${dpr}: ${name} ${length}`).toBe(true);
                }
                // The major is the minor's multiple exactly, so the two levels share lines.
                expect(geometry.major).toBeCloseTo(geometry.minor * SCENE_MAJOR, 9);
                // The minor is four scanline rows exactly, so the full rows land on faint ones.
                expect(geometry.minor).toBeCloseTo(geometry.fine * 4, 9);
                // Within two device pixels of the preset.
                expect(Math.abs(geometry.minor - pitch) * dpr).toBeLessThanOrEqual(2 + 1e-9);
            }
        }
    });

    it('keeps the presets as they are where they already land on the grid', () => {
        expect(sceneGeometry(16, 1)).toEqual({ minor: 16, major: 80, hair: 1, fine: 4 });
        expect(sceneGeometry(16, 1.25)).toEqual({ minor: 16, major: 80, hair: 0.8, fine: 4 });
        expect(sceneGeometry(20, 2)).toEqual({ minor: 20, major: 100, hair: 1, fine: 5 });
    });

    it('snaps a pitch that would land between device pixels to whole scanline rows', () => {
        // 17.6 device px: rows of 4, so a minor of 16 and a major of 80, which
        // every full row shares with a faint one (a minor snapped on its own,
        // 18, put the 90px major between the 88 and 92 rows).
        const at = sceneGeometry(16, 1.1);
        expect(at.fine * 1.1).toBeCloseTo(4, 9);
        expect(at.minor * 1.1).toBeCloseTo(16, 9);
        expect(at.major * 1.1).toBeCloseTo(80, 9);
        expect(at.hair * 1.1).toBeCloseTo(1, 9);
        // A user skin's 20 at 1.5×: 7.5 device px rows round to 8, the minor to 32.
        const wide = sceneGeometry(20, 1.5);
        expect(wide.fine * 1.5).toBeCloseTo(8, 9);
        expect(wide.major * 1.5).toBeCloseTo(160, 9);
    });
});

describe('scene layers', () => {
    it('a flat ground has no layers', () => {
        expect(sceneBackground(FLAT, PAINT, g)).toBeNull();
    });

    it('a grid rules both axes at the major pitch in the ink and at the minor in the faint ink', () => {
        const style = sceneBackground({ ...FLAT, pattern: 'grid' }, PAINT, g)!;
        expect(style.backgroundImage.split('), ')).toHaveLength(4);
        expect(style.backgroundImage).toContain('linear-gradient(90deg, INK 0 1px, transparent 1px)');
        expect(style.backgroundImage).toContain('linear-gradient(180deg, FAINT 0 1px, transparent 1px)');
        expect(style.backgroundSize).toBe('80px 80px, 80px 80px, 16px 16px, 16px 16px');
        expect(style.backgroundRepeat).toBe('repeat, repeat, repeat, repeat');
    });

    it('dots mark the major and minor crossings with squares two hairlines wide', () => {
        const style = sceneBackground({ ...FLAT, pattern: 'dots' }, PAINT, g)!;
        expect(style.backgroundImage).toBe(
            'conic-gradient(from 270deg at 2px 2px, INK 0 25%, transparent 25% 100%), '
            + 'conic-gradient(from 270deg at 2px 2px, FAINT 0 25%, transparent 25% 100%)');
        expect(style.backgroundSize).toBe('80px 80px, 16px 16px');
    });

    it('fine dots mark every minor crossing with a one-hairline square, one level only', () => {
        const style = sceneBackground({ ...FLAT, pattern: 'fine-dots' }, PAINT, g)!;
        expect(style.backgroundImage).toBe('conic-gradient(from 270deg at 1px 1px, INK 0 25%, transparent 25% 100%)');
        expect(style.backgroundSize).toBe('16px 16px');
        expect(style.backgroundRepeat).toBe('repeat');
        // The one layer lies over the vignette and the glows.
        const lit = sceneBackground({
            pattern: 'fine-dots', glowWarm: { color: '#ffffff', alpha: 0.2 }, glowCool: null, vignette: 0.5, image: null,
        }, PAINT, g)!;
        expect(lit.backgroundImage).toMatch(/^conic-gradient\(from 270deg at 1px 1px, INK 0 25%, transparent 25% 100%\), radial-gradient\([^)]*SHADE/);
        expect(lit.backgroundImage).not.toContain('FAINT');
        expect(lit.backgroundSize).toBe('16px 16px, 100% 100%, 100% 100%');
        expect(lit.backgroundRepeat).toBe('repeat, no-repeat, no-repeat');
    });

    it('scanlines rule rows only: the major pitch in the ink, a quarter of the minor in the faint ink', () => {
        const style = sceneBackground({ ...FLAT, pattern: 'scanlines' }, PAINT, g)!;
        expect(style.backgroundImage).not.toContain('90deg');
        expect(style.backgroundSize).toBe('100% 80px, 100% 4px');
        expect(style.backgroundRepeat).toBe('repeat-y, repeat-y');
    });

    it('a picture lies under everything, covering the ground from its centre, and alone is a layer', () => {
        const picture = { ...FLAT, image: 'a'.repeat(64) };
        const alone = sceneBackground(picture, PAINT, g)!;
        expect(alone).toEqual({
            backgroundImage: 'PICTURE', backgroundSize: 'cover', backgroundRepeat: 'no-repeat', backgroundPosition: 'center',
        });
        const lit = sceneBackground({ ...picture, pattern: 'grid', vignette: 0.5, glowCool: { color: '#000000', alpha: 0.3 } }, PAINT, g)!;
        const images = lit.backgroundImage.split('), ');
        expect(images).toHaveLength(7);
        expect(images.at(-1)).toBe('PICTURE');
        expect(lit.backgroundSize.split(', ').at(-1)).toBe('cover');
        expect(lit.backgroundPosition).toBe('0 0, 0 0, 0 0, 0 0, 0 0, 0 0, center');
        // No picture, no layer for it.
        expect(sceneBackground({ ...FLAT, pattern: 'grid' }, PAINT, g)!.backgroundImage).not.toContain('PICTURE');
    });

    it('the vignette and the glows lie under the pattern, each once over the whole ground', () => {
        const scene: Pick<ThemeScene, 'pattern' | 'glowWarm' | 'glowCool' | 'vignette' | 'image'> = {
            pattern: 'grid', glowWarm: { color: '#ffffff', alpha: 0.2 }, glowCool: { color: '#000000', alpha: 0.3 }, vignette: 0.5, image: null,
        };
        const style = sceneBackground(scene, PAINT, g)!;
        const images = style.backgroundImage;
        expect(images.indexOf('INK')).toBeLessThan(images.indexOf('SHADE'));
        expect(images.indexOf('SHADE')).toBeLessThan(images.indexOf('WARM'));
        expect(images.indexOf('WARM')).toBeLessThan(images.indexOf('COOL'));
        expect(style.backgroundSize.split(', ').slice(-3)).toEqual(['100% 100%', '100% 100%', '100% 100%']);
        expect(style.backgroundRepeat.split(', ').slice(-3)).toEqual(['no-repeat', 'no-repeat', 'no-repeat']);
        // Only what the scene has.
        expect(sceneBackground({ ...FLAT, glowCool: scene.glowCool }, PAINT, g)!.backgroundImage).toMatch(/^radial-gradient\([^)]*at 105% 95%, COOL/);
    });
});
