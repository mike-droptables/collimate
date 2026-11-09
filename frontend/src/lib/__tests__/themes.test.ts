import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, it, expect } from "vitest";
import {
  THEMES,
  BUILTIN_THEME_IDS,
  DEFAULT_THEME_ID,
  THEME_CSS_VARS,
  THEME_VAR_KEYS,
  SCENE_CSS_VARS,
  SCENE_PATTERNS,
  SCENE_PITCHES,
  SELECTION_EDGES,
  getTheme,
  isSceneImage,
  themeCssVars,
  sceneCssVars,
  sceneChromeFill,
  sceneGround,
  softVariant,
  type ThemeId,
  type ThemeScene,
} from "../themes";
import { newUserTheme, userThemeToDef } from "../userThemes";
import { themeImageUrl } from "../api/appSettings";
import { DEFAULT_FAMILIES, FONT_CATALOGUE, defaultFamily, stackFor, familyForStack } from "../fonts";

const EXPECTED_CSS_NAMES = [
  "--canvas", "--sheet", "--sheet-2", "--sheet-3", "--line", "--line-2",
  "--ink", "--ink-2", "--ink-3", "--brass", "--brass-soft", "--teal",
  "--teal-soft", "--teal-sheet", "--red", "--red-soft", "--green",
  "--code-bg", "--prose-ink", "--shadow", "--lift", "--font-ui",
  "--font-mono", "--font-prose", "--radius",
];

describe("skins", () => {
  it("ships the built-ins in card order, graphite first and the default", () => {
    const ids = ["graphite", "night", "night-cool", "night-clear", "grid", "cyberpunk", "paper", "precog"];
    expect(THEMES.map((t) => t.id)).toEqual(ids);
    expect([...BUILTIN_THEME_IDS]).toEqual(ids);
    expect(THEMES.map((t) => t.scheme)).toEqual(["dark", "dark", "dark", "dark", "dark", "dark", "light", "light"]);
    expect(new Set(THEMES.map((t) => t.label)).size).toBe(THEMES.length);
    expect(DEFAULT_THEME_ID).toBe("graphite");
    expect(THEMES[0].id).toBe(DEFAULT_THEME_ID);
  });

  it("every skin carries every token, and nothing else (S01)", () => {
    for (const theme of THEMES) {
      expect(Object.keys(theme.vars).sort()).toEqual([...THEME_VAR_KEYS].sort());
      for (const key of THEME_VAR_KEYS) {
        expect(theme.vars[key], `${theme.id}.${key}`).toMatch(/\S/);
      }
    }
  });

  it("the CSS-name table names every token exactly once (S01)", () => {
    expect(Object.values(THEME_CSS_VARS)).toEqual(EXPECTED_CSS_NAMES);
    expect(Object.keys(themeCssVars(getTheme("paper").vars))).toEqual(EXPECTED_CSS_NAMES);
  });

  it("carries the mock's values under the product's skin names (S01)", () => {
    // Mock `warm` is the product's night, mock `night` is night-cool.
    expect(getTheme("night").vars).toMatchObject({ canvas: "#151311", brass: "#c69d64", codeBg: "#121008" });
    expect(getTheme("night-cool").vars).toMatchObject({ canvas: "#10161b", teal: "#76a9a6" });
    expect(getTheme("paper").vars).toMatchObject({ canvas: "#e6e4de", tealSoft: "rgba(46, 119, 115, 0.10)" });
    for (const id of ["night", "night-cool", "paper"] as const) {
      expect(getTheme(id).vars).toMatchObject({
        fontUi: expect.stringMatching(/^"Hanken Grotesk", /),
        fontProse: expect.stringMatching(/^"Lora", .*serif$/),
        fontMono: expect.stringMatching(/^"JetBrains Mono", .*monospace$/),
        radius: "8px",
      });
    }
  });

  it("a built-in sets its own radius and fonts only from the catalogue", () => {
    for (const theme of THEMES) {
      expect(theme.vars.radius, theme.id).toMatch(/^\d+px$/);
      for (const stack of [theme.vars.fontUi, theme.vars.fontMono, theme.vars.fontProse]) {
        expect(familyForStack(stack)?.stack, `${theme.id} ${stack}`).toBe(stack);
      }
    }
    expect(getTheme("cyberpunk").vars).toMatchObject({ radius: "2px", fontUi: stackFor("Geist Mono") });
    expect(getTheme("precog").vars).toMatchObject({ radius: "12px", fontUi: stackFor("Manrope") });
  });

  it("globals.css paints the default skin's tokens before the first theme applies (S01)", () => {
    const css = readFileSync(resolve(__dirname, "../../app/globals.css"), "utf8");
    const root = /\n:root \{([^}]*)\}/.exec(css)?.[1] ?? "";
    const declared = Object.fromEntries(
      [...root.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]),
    );
    expect(declared).toEqual(themeCssVars(getTheme(DEFAULT_THEME_ID).vars));
  });

  it("getTheme falls back to the default skin (S01)", () => {
    expect(getTheme("paper").id).toBe("paper");
    expect(getTheme("no-such-skin" as ThemeId).id).toBe(DEFAULT_THEME_ID);
  });

  it("a user skin is its base's tokens under its overrides (S01)", () => {
    const user = { ...newUserTheme("paper", "Mine"), vars: { ink: "#123456" } };
    expect(user.id).toMatch(/^user\./);
    expect(user.baseThemeId).toBe("paper");
    const def = userThemeToDef(user);
    expect(def.scheme).toBe("light");
    expect(def.vars).toEqual({ ...getTheme("paper").vars, ink: "#123456" });
  });

  it("night clear is night made clearer: lighter grounds, brighter lines, stronger brass, a 2px edge", () => {
    const night = getTheme("night");
    const clear = getTheme("night-clear");
    for (const key of ["canvas", "sheet", "sheet2", "sheet3", "line", "line2", "brass"] as const) {
      expect(luminance(clear.vars[key]), key).toBeGreaterThan(luminance(night.vars[key]));
    }
    expect(alphaOf(clear.vars.brassSoft)).toBeGreaterThan(alphaOf(night.vars.brassSoft));
    expect(clear.scene.edge).toBe(2);
    expect(night.scene.edge).toBe(1);
  });

  it("every built-in keeps the token semantics legible: each accent clears 3:1 on both code grounds", () => {
    // syntaxPalette (the SDK) nudges a role below 3:1 on --sheet or --code-bg;
    // a built-in's accents and inks clear it as they are, so code shows the
    // skin's own brass, teal, red and green.
    for (const { id, vars } of THEMES) {
      for (const key of ["ink", "ink2", "brass", "teal", "red", "green"] as const) {
        for (const ground of ["sheet", "codeBg"] as const) {
          expect(contrast(vars[key], vars[ground]), `${id} ${key} on ${ground}`).toBeGreaterThanOrEqual(3);
        }
      }
      // Selection and live read apart.
      expect(vars.brass, id).not.toBe(vars.teal);
    }
  });

  it("softVariant keeps the soft token's alpha (S01)", () => {
    expect(softVariant("#102030", "rgba(1, 2, 3, 0.16)")).toBe("rgba(16, 32, 48, 0.16)");
    expect(softVariant("teal", "rgba(1, 2, 3, 0.16)")).toBe("rgba(1, 2, 3, 0.16)");
  });
});

describe("font catalogue", () => {
  it("offers the Google faces and the ebook-fonts core per role (S01)", () => {
    expect(FONT_CATALOGUE.ui.map((f) => f.family)).toEqual([
      "Hanken Grotesk", "Inter", "IBM Plex Sans", "Source Sans 3", "Manrope", "NV Jost", "NV Legible Next",
    ]);
    expect(FONT_CATALOGUE.prose.map((f) => f.family)).toEqual([
      "Alegreya", "Source Serif 4", "Literata", "Lora", "Crimson Pro",
      "Libron", "Sourcerer", "Cartisse", "Charis SIL", "NV Garamond", "NV Bitter", "NV Palatium",
    ]);
    expect(FONT_CATALOGUE.mono.map((f) => f.family)).toEqual(["IBM Plex Mono", "JetBrains Mono", "Source Code Pro", "Fira Code", "Geist Mono"]);
    for (const role of ["ui", "mono", "prose"] as const) {
      for (const f of FONT_CATALOGUE[role]) expect(f.role).toBe(role);
    }
  });

  it("maps a family to its stack and a stack back to its family (S01)", () => {
    expect(stackFor("Lora")).toBe('"Lora", Georgia, "Times New Roman", serif');
    expect(stackFor("Comic Sans")).toBeUndefined();
    expect(familyForStack(getTheme("night").vars.fontMono)?.family).toBe("JetBrains Mono");
  });

  it("names Lora for prose and JetBrains Mono for code as the defaults, by name and not by position (polish, RS-05)", () => {
    expect(DEFAULT_FAMILIES).toEqual({ ui: "Hanken Grotesk", prose: "Lora", mono: "JetBrains Mono" });
    for (const role of ["ui", "mono", "prose"] as const) {
      const f = defaultFamily(role);
      expect(f.role).toBe(role);
      expect(f.family).toBe(DEFAULT_FAMILIES[role]);
      expect(FONT_CATALOGUE[role]).toContain(f);
    }
    // Neither default leads its role's list, so the lookup is by name.
    expect(FONT_CATALOGUE.prose[0].family).not.toBe("Lora");
    expect(FONT_CATALOGUE.mono[0].family).not.toBe("JetBrains Mono");
  });

  it("every built-in skin sets prose in Lora and code in JetBrains Mono, and a user skin inherits them (polish, RS-05)", () => {
    for (const theme of THEMES) {
      expect(familyForStack(theme.vars.fontProse)?.family, theme.id).toBe("Lora");
      expect(familyForStack(theme.vars.fontMono)?.family, theme.id).toBe("JetBrains Mono");
      expect(theme.vars.fontProse).toBe(stackFor("Lora"));
      expect(theme.vars.fontMono).toBe(stackFor("JetBrains Mono"));
    }
    for (const base of BUILTIN_THEME_IDS) {
      const inherited = userThemeToDef(newUserTheme(base, "Mine")).vars;
      expect([inherited.fontProse, inherited.fontMono], base).toEqual([stackFor("Lora"), stackFor("JetBrains Mono")]);
    }
  });
});

/** WCAG relative luminance of a #rrggbb colour. */
function luminance(hex: string): number {
  const channel = (i: number) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function alphaOf(rgba: string): number {
  return Number(/,\s*([\d.]+)\s*\)$/.exec(rgba)?.[1]);
}

describe("scenes", () => {
  const isHex = (v: unknown) => typeof v === "string" && /^#[0-9a-f]{6}$/i.test(v);

  it("every built-in carries a whole scene with values from the menus and no picture; only graphite reaches behind chrome", () => {
    const keys: (keyof ThemeScene)[] = [
      "base", "pattern", "ink", "pitch", "strength", "glowWarm", "glowCool", "vignette", "edge", "behindTopBar", "behindRemoved", "image",
    ];
    for (const { id, scene } of THEMES) {
      expect(Object.keys(scene).sort(), id).toEqual([...keys].sort());
      expect(scene.base === null || isHex(scene.base), id).toBe(true);
      expect(SCENE_PATTERNS, id).toContain(scene.pattern);
      expect(SCENE_PITCHES, id).toContain(scene.pitch);
      expect(SELECTION_EDGES, id).toContain(scene.edge);
      expect(isHex(scene.ink), id).toBe(true);
      expect(scene.image, id).toBeNull();
      expect([scene.behindTopBar, scene.behindRemoved], id).toEqual([false, id === "graphite"]);
      for (const unit of [scene.strength, scene.vignette]) expect(unit >= 0 && unit <= 1, id).toBe(true);
      for (const glow of [scene.glowWarm, scene.glowCool]) {
        if (glow) expect(isHex(glow.color) && glow.alpha >= 0 && glow.alpha <= 1, id).toBe(true);
      }
    }
  });

  it("graphite, the default, is fine dots at 24/120 at 55% on its canvas, running on behind the removed shelf", () => {
    const { scene, vars } = getTheme("graphite");
    expect(scene).toMatchObject({
      base: null, pattern: "fine-dots", pitch: 24, strength: 0.55, glowWarm: null, glowCool: null, vignette: 0,
      edge: 1, behindTopBar: false, behindRemoved: true, image: null,
    });
    expect(sceneGround(scene, vars)).toBe(vars.canvas);
    expect(isSceneImage(scene.ink)).toBe(false);
  });

  it("night, night-cool and paper are flat on their canvas", () => {
    for (const id of ["night", "night-cool", "paper"] as const) {
      const { scene, vars } = getTheme(id);
      expect(scene).toMatchObject({ base: null, pattern: "none", glowWarm: null, glowCool: null, vignette: 0, edge: 1 });
      expect(sceneGround(scene, vars)).toBe(vars.canvas);
    }
  });

  it("the grid skin stands dark cards on a lighter ground of its own, ruled at 16/80 with glows and a vignette", () => {
    const { scene, vars } = getTheme("grid");
    expect(scene).toMatchObject({ pattern: "grid", pitch: 16 });
    expect(scene.glowWarm).not.toBeNull();
    expect(scene.glowCool).not.toBeNull();
    expect(scene.vignette).toBeGreaterThan(0);
    expect(luminance(sceneGround(scene, vars))).toBeGreaterThan(luminance(vars.sheet));
    expect(luminance(vars.canvas)).toBeLessThan(luminance(vars.sheet));
  });

  it("writes complete colours under the scene's own names: the ink at the strength, the minor at half", () => {
    const vars = getTheme("night").vars;
    const scene: ThemeScene = {
      base: "#102030", pattern: "grid", ink: "#ffffff", pitch: 20, strength: 0.4,
      glowWarm: { color: "#ff0000", alpha: 1 }, glowCool: null, vignette: 0.5, edge: 2,
      behindTopBar: false, behindRemoved: false, image: null,
    };
    expect(sceneCssVars(scene, vars)).toEqual({
      [SCENE_CSS_VARS.ground]: "#102030",
      [SCENE_CSS_VARS.ink]: "#ffffff66",
      [SCENE_CSS_VARS.inkFaint]: "#ffffff33",
      [SCENE_CSS_VARS.glowWarm]: "#ff0000ff",
      [SCENE_CSS_VARS.glowCool]: "transparent",
      [SCENE_CSS_VARS.vignette]: "#00000080",
      [SCENE_CSS_VARS.edge]: "2px",
      [SCENE_CSS_VARS.topBarGround]: vars.canvas,
      [SCENE_CSS_VARS.removedGround]: vars.canvas,
      [SCENE_CSS_VARS.image]: "none",
    });
    expect(sceneCssVars({ ...scene, base: null }, vars)[SCENE_CSS_VARS.ground]).toBe(vars.canvas);
    // A picture is its URL in the server's app store as a CSS image, beside the API.
    const hash = "a".repeat(64);
    expect(isSceneImage(hash)).toBe(true);
    expect(isSceneImage(hash.toUpperCase())).toBe(false);
    expect(isSceneImage("data:x")).toBe(false);
    expect(sceneCssVars({ ...scene, image: hash }, vars)[SCENE_CSS_VARS.image]).toBe(`url("${themeImageUrl(hash)}")`);
    expect(themeImageUrl(hash)).toMatch(new RegExp(`/api/app-settings/images/${hash}$`));
    // A reach option makes its chrome's fill transparent, each on its own.
    const reach = (over: Partial<ThemeScene>) => sceneCssVars({ ...scene, ...over }, vars);
    expect(reach({ behindTopBar: true })[SCENE_CSS_VARS.topBarGround]).toBe("transparent");
    expect(reach({ behindTopBar: true })[SCENE_CSS_VARS.removedGround]).toBe(vars.canvas);
    expect(reach({ behindRemoved: true })[SCENE_CSS_VARS.removedGround]).toBe("transparent");
    expect(reach({ behindRemoved: true })[SCENE_CSS_VARS.topBarGround]).toBe(vars.canvas);
    // The chrome reads them as var()s that fall back to the canvas.
    expect(sceneChromeFill(SCENE_CSS_VARS.topBarGround)).toBe("var(--scene-top-bar-ground, var(--canvas))");
    expect(sceneChromeFill(SCENE_CSS_VARS.removedGround)).toBe("var(--scene-removed-ground, var(--canvas))");
    // The scene is host-only: none of its names is a skin token.
    for (const name of Object.values(SCENE_CSS_VARS)) expect(Object.values(THEME_CSS_VARS)).not.toContain(name);
  });
});
