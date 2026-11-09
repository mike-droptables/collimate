// The scene's background layers: one element's CSS backgrounds that draw a
// skin's pattern, glows and vignette over its ground (lib/themes ThemeScene).
// SceneBackground paints them behind the stacks with the colours from their
// host CSS properties and a geometry snapped to the device pixel grid; the
// theme editor's previews paint them with a skin's own colours.

import { SCENE_CSS_VARS, SCENE_MAJOR, ScenePitch, ThemeScene } from './themes';

/** The pattern's lengths in CSS px, each a whole number of device pixels. */
export interface SceneGeometry {
    /** The minor pitch. */
    minor: number;
    /** The major pitch: SCENE_MAJOR minor pitches exactly, so the two
     *  levels share their lines. */
    major: number;
    /** A hairline: about one CSS px, in whole device pixels (at least one). */
    hair: number;
    /** The scanlines' row pitch, a quarter of the minor. */
    fine: number;
}

/** Whole device pixels in `css` CSS px at `dpr`, at least `floor`. */
function devicePx(css: number, dpr: number, floor: number): number {
    return Math.max(floor, Math.round(css * dpr)) / dpr;
}

/** `pitch` snapped to whole device pixels at `dpr`: a period that is not one
 *  draws every few lines a pixel wider (the 75/15 beat of the flicker note).
 *  Every period is a whole number of one quantum, the scanline row (a quarter
 *  pitch in whole device pixels): the minor is four rows and the major
 *  SCENE_MAJOR minors, so the levels share their lines at every scale (a
 *  minor snapped on its own, 18 device px at 1.1, leaves 4px rows that the
 *  90px major falls between). The minor lands within two device pixels. */
export function sceneGeometry(pitch: ScenePitch, dpr: number): SceneGeometry {
    const fine = devicePx(pitch / 4, dpr, 2);
    const minor = fine * 4;
    return {
        minor,
        major: minor * SCENE_MAJOR,
        hair: devicePx(1, dpr, 1),
        fine,
    };
}

/** The colours the layers paint in, complete CSS colours or var() references. */
export interface ScenePaint {
    /** The major marks, and a one-level pattern's only marks. */
    ink: string;
    /** The minor marks of a two-level pattern. */
    inkFaint: string;
    glowWarm: string;
    glowCool: string;
    vignette: string;
    /** The picture under everything else: a `url()` image, or `none`. */
    image: string;
}

/** The paint read from the scene's host properties, through `read`: their
 *  `var()` references (the live scene) or their values (a preview). */
export function scenePaint(read: (name: string) => string): ScenePaint {
    return {
        ink: read(SCENE_CSS_VARS.ink),
        inkFaint: read(SCENE_CSS_VARS.inkFaint),
        glowWarm: read(SCENE_CSS_VARS.glowWarm),
        glowCool: read(SCENE_CSS_VARS.glowCool),
        vignette: read(SCENE_CSS_VARS.vignette),
        image: read(SCENE_CSS_VARS.image),
    };
}

/** The parts of a scene that decide which layers exist. */
export type SceneShape = Pick<ThemeScene, 'pattern' | 'glowWarm' | 'glowCool' | 'vignette' | 'image'>;

export interface SceneBackgroundStyle {
    backgroundImage: string;
    backgroundSize: string;
    backgroundRepeat: string;
    backgroundPosition: string;
}

/** One background; a tile starts at the element's origin unless `position`
 *  says where. */
interface Layer { image: string; size: string; repeat: string; position?: string }

const px = (v: number) => `${v}px`;
const tile = (w: string, h: string = w) => `${w} ${h}`;

/** A rule along each tile's top edge (`180deg`) or left edge (`90deg`). */
function rule(direction: '90deg' | '180deg', color: string, hair: number): string {
    return `linear-gradient(${direction}, ${color} 0 ${px(hair)}, transparent ${px(hair)})`;
}

/** A square dot of side `size` in each tile's top-left corner. */
function dot(color: string, size: number): string {
    return `conic-gradient(from 270deg at ${px(size)} ${px(size)}, ${color} 0 25%, transparent 25% 100%)`;
}

function patternLayers(pattern: ThemeScene['pattern'], paint: ScenePaint, g: SceneGeometry): Layer[] {
    const major = tile(px(g.major));
    const minor = tile(px(g.minor));
    switch (pattern) {
        case 'grid':
            return [
                { image: rule('90deg', paint.ink, g.hair), size: major, repeat: 'repeat' },
                { image: rule('180deg', paint.ink, g.hair), size: major, repeat: 'repeat' },
                { image: rule('90deg', paint.inkFaint, g.hair), size: minor, repeat: 'repeat' },
                { image: rule('180deg', paint.inkFaint, g.hair), size: minor, repeat: 'repeat' },
            ];
        case 'dots':
            return [
                { image: dot(paint.ink, g.hair * 2), size: major, repeat: 'repeat' },
                { image: dot(paint.inkFaint, g.hair * 2), size: minor, repeat: 'repeat' },
            ];
        case 'fine-dots':
            // One level: a hairline square at every minor crossing, in the
            // full ink since there is no second level to be faint against.
            return [{ image: dot(paint.ink, g.hair), size: minor, repeat: 'repeat' }];
        case 'scanlines':
            return [
                { image: rule('180deg', paint.ink, g.hair), size: tile('100%', px(g.major)), repeat: 'repeat-y' },
                { image: rule('180deg', paint.inkFaint, g.hair), size: tile('100%', px(g.fine)), repeat: 'repeat-y' },
            ];
        case 'none':
            return [];
    }
}

/** The scene's layers, topmost first: the pattern's major then minor marks
 *  (fine dots have one level, a hairline square at every minor pitch), the
 *  vignette, then the warm glow (top centre) and the cool glow (bottom
 *  right), and under them all the picture, covering the ground from its
 *  centre. Null when the scene is a flat ground. Every tile starts at the
 *  element's origin, so the caller keeps that origin on a device pixel. */
export function sceneBackground(scene: SceneShape, paint: ScenePaint, g: SceneGeometry): SceneBackgroundStyle | null {
    const whole = { size: tile('100%'), repeat: 'no-repeat' };
    const layers: Layer[] = [
        ...patternLayers(scene.pattern, paint, g),
        ...(scene.vignette > 0
            ? [{ image: `radial-gradient(ellipse at center, transparent 0%, ${paint.vignette} 100%)`, ...whole }]
            : []),
        ...(scene.glowWarm
            ? [{ image: `radial-gradient(ellipse 140% 90% at 50% 5%, ${paint.glowWarm} 0%, transparent 65%)`, ...whole }]
            : []),
        ...(scene.glowCool
            ? [{ image: `radial-gradient(ellipse 100% 80% at 105% 95%, ${paint.glowCool} 0%, transparent 60%)`, ...whole }]
            : []),
        ...(scene.image
            ? [{ image: paint.image, size: 'cover', repeat: 'no-repeat', position: 'center' }]
            : []),
    ];
    if (layers.length === 0) return null;
    return {
        backgroundImage: layers.map(l => l.image).join(', '),
        backgroundSize: layers.map(l => l.size).join(', '),
        backgroundRepeat: layers.map(l => l.repeat).join(', '),
        backgroundPosition: layers.map(l => l.position ?? '0 0').join(', '),
    };
}
