import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, it, expect } from "vitest";
import {
  THEMES,
  BUILTIN_THEME_IDS,
  DEFAULT_THEME_ID,
  THEME_CSS_VARS,
  THEME_VAR_KEYS,
  getTheme,
  themeCssVars,
  softVariant,
  type ThemeId,
} from "../themes";
import { newUserTheme, userThemeToDef } from "../userThemes";
import { DEFAULT_FAMILIES, FONT_CATALOGUE, defaultFamily, stackFor, familyForStack } from "../fonts";

const EXPECTED_CSS_NAMES = [
  "--canvas", "--sheet", "--sheet-2", "--sheet-3", "--line", "--line-2",
  "--ink", "--ink-2", "--ink-3", "--brass", "--brass-soft", "--teal",
  "--teal-soft", "--teal-sheet", "--red", "--red-soft", "--green",
  "--code-bg", "--prose-ink", "--shadow", "--lift", "--font-ui",
  "--font-mono", "--font-prose", "--radius",
];

describe("skins", () => {
  it("ships night, night-cool and paper with night the default (S01)", () => {
    expect(THEMES.map((t) => t.id)).toEqual(["night", "night-cool", "paper"]);
    expect([...BUILTIN_THEME_IDS]).toEqual(["night", "night-cool", "paper"]);
    expect(THEMES.map((t) => t.scheme)).toEqual(["dark", "dark", "light"]);
    expect(DEFAULT_THEME_ID).toBe("night");
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
    for (const theme of THEMES) {
      expect(theme.vars).toMatchObject({
        fontUi: expect.stringMatching(/^"Hanken Grotesk", /),
        fontProse: expect.stringMatching(/^"Lora", .*serif$/),
        fontMono: expect.stringMatching(/^"JetBrains Mono", .*monospace$/),
        radius: "8px",
      });
    }
  });

  it("globals.css paints night's tokens before the first theme applies (S01)", () => {
    const css = readFileSync(resolve(__dirname, "../../app/globals.css"), "utf8");
    const root = /\n:root \{([^}]*)\}/.exec(css)?.[1] ?? "";
    const declared = Object.fromEntries(
      [...root.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]),
    );
    expect(declared).toEqual(themeCssVars(getTheme("night").vars));
  });

  it("getTheme falls back to night (S01)", () => {
    expect(getTheme("paper").id).toBe("paper");
    expect(getTheme("no-such-skin" as ThemeId).id).toBe("night");
  });

  it("a user skin is its base's tokens under its overrides (S01)", () => {
    const user = { ...newUserTheme("paper", "Mine"), vars: { ink: "#123456" } };
    expect(user.id).toMatch(/^user\./);
    expect(user.baseThemeId).toBe("paper");
    const def = userThemeToDef(user);
    expect(def.scheme).toBe("light");
    expect(def.vars).toEqual({ ...getTheme("paper").vars, ink: "#123456" });
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
