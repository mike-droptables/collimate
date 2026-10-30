#!/usr/bin/env node
/**
 * Bundles the font catalogue as self-hosted woff2.
 *
 * Reads FONT_CATALOGUE from src/lib/fonts.ts and, for each family, by its
 * source:
 *   - google: asks the Google Fonts css2 API (with a modern Chrome user
 *     agent, so it answers with woff2) for the family's weights, as one
 *     variable file per style where the family is variable, otherwise one
 *     file per weight; keeps the latin and latin-ext subsets and saves their
 *     files under public/fonts/<slug>/, beside the family's OFL.txt from
 *     google/fonts.
 *   - github: downloads the family's four static TTFs from ebook-fonts at the
 *     pinned commit, checks each declares the catalogue's family, weight and
 *     style, subsets it to the latin and latin-ext ranges with fontTools
 *     (every OpenType feature and name kept) as one woff2 per style, and
 *     writes LICENSE.txt: the font's own copyright and licence strings, the
 *     catalogue's note on how they read where they differ from the licence,
 *     then the upstream licence texts the catalogue names.
 * Then writes public/fonts/fonts.css (one @font-face per file, font-display:
 * swap, the file's unicode-range) and the BUNDLED FONTS section of
 * THIRD_PARTY_LICENSES, and prints the size per family.
 *
 * Usage: node scripts/build_fonts.mjs   (needs network and python3 with
 * fontTools and brotli; commit the output)
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { FONT_CATALOGUE } from "../src/lib/fonts.ts";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const FONTS_DIR = path.join(ROOT, "public", "fonts");
const LICENSES_PATH = path.join(ROOT, "..", "THIRD_PARTY_LICENSES");

const CHROME_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
const SUBSETS = ["latin-ext", "latin"];
const LICENSE_HEADER = "BUNDLED FONTS\n=============";
const SEPARATOR = "-".repeat(79);

/** Google Fonts' latin and latin-ext unicode ranges, which a github family is
 *  subset to. */
const SUBSET_UNICODES = [
  "U+0000-00FF", "U+0131", "U+0152-0153", "U+02BB-02BC", "U+02C6", "U+02DA", "U+02DC", "U+0304", "U+0308",
  "U+0329", "U+2000-206F", "U+20AC", "U+2122", "U+2191", "U+2193", "U+2212", "U+2215", "U+FEFF", "U+FFFD",
  "U+0100-02BA", "U+02BD-02C5", "U+02C7-02CC", "U+02CE-02D7", "U+02DD-02FF", "U+1D00-1DBF", "U+1E00-1E9F",
  "U+1EF2-1EFF", "U+2020", "U+20A0-20AB", "U+20AD-20C4", "U+2113", "U+2C60-2C7F", "U+A720-A7FF",
].join(",");

/** The four static styles of a github family. */
const STYLES = [
  { key: "regular", style: "normal", weight: 400 },
  { key: "italic", style: "italic", weight: 400 },
  { key: "bold", style: "normal", weight: 700 },
  { key: "boldItalic", style: "italic", weight: 700 },
];

const LICENSE_NAMES = {
  "OFL-1.1": "SIL Open Font License 1.1",
  "Bitstream-Charter": "Bitstream Charter Font License",
  "OFL-1.1 OR LPPL-1.3c OR AGPL-3.0-only WITH LicenseRef-URW-font-exception":
    "at your choice, the SIL Open Font License 1.1, the LaTeX Project Public License 1.3c, " +
    "or the GNU Affero General Public License 3.0 with URW's font exception",
};

/** Reads a TTF's family, weight, style and licence strings, and the code
 *  points its woff2 subset maps, merged into ranges. */
const FONT_FACTS = `
import json, sys
from fontTools.ttLib import TTFont
ttf, woff2 = TTFont(sys.argv[1]), TTFont(sys.argv[2])
names = ttf["name"]
def name(i):
    r = names.getName(i, 3, 1, 0x409) or names.getName(i, 1, 0, 0)
    return r.toUnicode() if r else None
ranges = []
for cp in sorted(woff2.getBestCmap()):
    if ranges and ranges[-1][1] == cp - 1:
        ranges[-1][1] = cp
    else:
        ranges.append([cp, cp])
print(json.dumps({
    "family": name(16) or name(1),
    "weight": ttf["OS/2"].usWeightClass,
    "italic": bool(ttf["OS/2"].fsSelection & 1),
    "copyright": name(0),
    "license": name(13),
    "licenseUrl": name(14),
    "ranges": ranges,
}))
`;

async function get(url, as) {
  const res = await fetch(url, { headers: { "user-agent": CHROME_UA } });
  if (!res.ok) return { status: res.status };
  return { status: res.status, body: as === "bytes" ? Buffer.from(await res.arrayBuffer()) : await res.text() };
}

/** The css2 axis spec for `font`: a weight range per style, or every weight. */
function axisSpec(font, variable) {
  const lo = Math.min(...font.weights);
  const hi = Math.max(...font.weights);
  const weights = variable ? [`${lo}..${hi}`] : font.weights.map(String);
  if (!font.italic) return `wght@${weights.join(";")}`;
  const tuples = [0, 1].flatMap((ital) => weights.map((w) => `${ital},${w}`));
  return `ital,wght@${tuples.join(";")}`;
}

async function googleCss(font) {
  const family = font.source.family.replace(/ /g, "+");
  for (const variable of [true, false]) {
    const url = `https://fonts.googleapis.com/css2?family=${family}:${axisSpec(font, variable)}&display=swap`;
    const res = await get(url, "text");
    if (res.body) return res.body;
    if (!variable) throw new Error(`${font.family}: css2 answered ${res.status} for ${url}`);
  }
}

/** The @font-face blocks of a css2 response, with the subset comment above each. */
function parseFaces(css) {
  const faces = [];
  for (const m of css.matchAll(/\/\*\s*([\w-]+)\s*\*\/\s*@font-face\s*\{([^}]*)\}/g)) {
    const prop = (name) => m[2].match(new RegExp(`${name}:\\s*([^;]+);`))?.[1].trim();
    faces.push({
      subset: m[1],
      style: prop("font-style"),
      weight: prop("font-weight"),
      url: prop("src").match(/url\(([^)]+)\)/)[1],
      unicodeRange: prop("unicode-range"),
    });
  }
  return faces;
}

/** The family's directory under public/fonts/, emptied. */
function freshDir(font) {
  const dir = path.join(FONTS_DIR, font.slug);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

async function bundleGoogle(font) {
  const dir = freshDir(font);
  const faces = parseFaces(await googleCss(font)).filter((f) => SUBSETS.includes(f.subset));
  if (faces.length === 0) throw new Error(`${font.family}: no latin faces in the css2 response`);
  const rules = [];
  let bytes = 0;
  for (const f of faces) {
    const file = `${font.slug}-${f.subset}-${f.style}-${f.weight.replace(/\s+/g, "-")}.woff2`;
    const res = await get(f.url, "bytes");
    if (!res.body) throw new Error(`${font.family}: ${res.status} for ${f.url}`);
    fs.writeFileSync(path.join(dir, file), res.body);
    bytes += res.body.length;
    rules.push(
      [
        `/* ${font.family} ${f.style} ${f.weight}, ${f.subset} */`,
        "@font-face {",
        `  font-family: '${font.family}';`,
        `  font-style: ${f.style};`,
        `  font-weight: ${f.weight};`,
        "  font-display: swap;",
        `  src: url(${font.slug}/${file}) format('woff2');`,
        `  unicode-range: ${f.unicodeRange};`,
        "}",
      ].join("\n"),
    );
  }

  const repoDir = font.source.family.toLowerCase().replace(/[^a-z0-9]/g, "");
  const ofl = await get(`https://raw.githubusercontent.com/google/fonts/main/ofl/${repoDir}/OFL.txt`, "text");
  if (!ofl.body) throw new Error(`${font.family}: no OFL.txt (${ofl.status})`);
  fs.writeFileSync(path.join(dir, "OFL.txt"), ofl.body);
  bytes += Buffer.byteLength(ofl.body);
  const copyright = ofl.body.split(/\r?\n\s*\r?\n/)[0].trim();

  return { font, rules, bytes, files: faces.length, copyright, licenseFile: "OFL.txt" };
}

function hex(cp) {
  return cp.toString(16).toUpperCase().padStart(4, "0");
}

function unicodeRange(ranges) {
  return ranges.map(([lo, hi]) => (lo === hi ? `U+${hex(lo)}` : `U+${hex(lo)}-${hex(hi)}`)).join(", ");
}

function indent(text, by) {
  return text.trim().split(/\r?\n/).map((line) => (line ? by + line : "")).join("\n");
}

/** `text` broken into lines of at most 79 columns: the first prefixed by
 *  `by`, the rest by `then`. */
function wrap(text, by, then = by) {
  const lines = [];
  let line = "";
  for (const word of text.split(/\s+/)) {
    if (line && (lines.length ? then : by).length + line.length + 1 + word.length > 79) {
      lines.push((lines.length ? then : by) + line);
      line = word;
    } else {
      line = line ? `${line} ${word}` : word;
    }
  }
  return [...lines, (lines.length ? then : by) + line].join("\n");
}

async function bundleGithub(font) {
  const { repo, ref, dir: repoDir, files: sourceFiles, origin } = font.source;
  const dir = freshDir(font);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), `fonts-${font.slug}-`));
  const rules = [];
  let bytes = 0;
  let notices;
  try {
    for (const { key, style, weight } of STYLES) {
      const url = `https://raw.githubusercontent.com/${repo}/${ref}/${repoDir}/${sourceFiles[key]}`;
      const res = await get(url, "bytes");
      if (!res.body) throw new Error(`${font.family}: ${res.status} for ${url}`);
      const ttf = path.join(tmp, sourceFiles[key]);
      fs.writeFileSync(ttf, res.body);

      const file = `${font.slug}-${style}-${weight}.woff2`;
      const out = path.join(dir, file);
      execFileSync("python3", [
        "-I", "-m", "fontTools.subset", ttf,
        "--flavor=woff2", "--layout-features=*", "--name-IDs=*",
        `--unicodes=${SUBSET_UNICODES}`, `--output-file=${out}`,
      ], { stdio: ["ignore", "ignore", "pipe"] });
      const facts = JSON.parse(execFileSync("python3", ["-I", "-c", FONT_FACTS, ttf, out], { encoding: "utf8" }));
      if (facts.family !== font.family || facts.weight !== weight || facts.italic !== (style === "italic")) {
        throw new Error(`${font.family}: ${sourceFiles[key]} declares ${facts.family} ${facts.weight}${facts.italic ? " italic" : ""}`);
      }
      notices ??= facts;
      bytes += fs.statSync(out).size;
      rules.push(
        [
          `/* ${font.family} ${style} ${weight}, latin and latin-ext */`,
          "@font-face {",
          `  font-family: '${font.family}';`,
          `  font-style: ${style};`,
          `  font-weight: ${weight};`,
          "  font-display: swap;",
          `  src: url(${font.slug}/${file}) format('woff2');`,
          `  unicode-range: ${unicodeRange(facts.ranges)};`,
          "}",
        ].join("\n"),
      );
    }
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }

  const upstream = [];
  for (const url of origin.licenseText) {
    const res = await get(url, "text");
    if (!res.body) throw new Error(`${font.family}: ${res.status} for ${url}`);
    upstream.push(`${url}\n\n${res.body.trim()}`);
  }
  const license = [
    font.family,
    "",
    `Origin: ${origin.family}`,
    `Modified by Nico Verbruggen: https://github.com/${repo}`,
    `  (commit ${ref}, ${repoDir}/)`,
    "Shipped by the app subset to the latin and latin-ext ranges, as woff2.",
    "",
    `License: ${font.license} (${LICENSE_NAMES[font.license]})`,
    "",
    "The font's copyright notice (name ID 0):",
    "",
    indent(notices.copyright, "  "),
    ...(notices.license
      ? ["", "The font's licence description (name ID 13):", "", indent(notices.license, "  ")]
      : []),
    ...(origin.note ? ["", wrap(origin.note, "")] : []),
    ...(notices.licenseUrl ? ["", `The font's licence URL (name ID 14): ${notices.licenseUrl}`] : []),
    "",
    ...upstream.flatMap((text) => [SEPARATOR, "", "The upstream licence text, from", text, ""]),
  ].join("\n");
  fs.writeFileSync(path.join(dir, "LICENSE.txt"), license);
  bytes += Buffer.byteLength(license);

  return { font, rules, bytes, files: STYLES.length, copyright: notices.copyright, licenseFile: "LICENSE.txt" };
}

function bundleFamily(font) {
  return font.source.provider === "google" ? bundleGoogle(font) : bundleGithub(font);
}

function licenseSection(results) {
  const entries = results.map(({ font, copyright, licenseFile }) =>
    [
      font.family,
      `  License: ${font.license} (${LICENSE_NAMES[font.license]})`,
      indent(copyright.replace(/\r?\n\s*\r?\n/g, "\n"), "  "),
      ...(font.source.provider === "github"
        ? [
          `  Origin: ${font.source.origin.family}, modified by Nico Verbruggen`,
          `    (https://github.com/${font.source.repo}, commit ${font.source.ref})`,
          ...(font.source.origin.note ? [wrap(`Note: ${font.source.origin.note}`, "  ", "    ")] : []),
        ]
        : []),
      `  Files: frontend/public/fonts/${font.slug}/ (the full licence is ${licenseFile} beside them)`,
    ].join("\n"),
  );
  return [
    LICENSE_HEADER,
    "",
    "The app bundles these font families as woff2 (frontend/public/fonts/,",
    "served at /fonts/). Families from Google Fonts are licensed under the SIL",
    "Open Font License 1.1 and ship their OFL.txt. Families from Nico",
    "Verbruggen's ebook-fonts are modified versions of the origin each entry",
    "names, subset to latin and latin-ext; each ships a LICENSE.txt with the",
    "font's own notices and the upstream licence text.",
    "",
    entries.join("\n\n"),
    "",
    SEPARATOR,
    "",
  ].join("\n");
}

function writeLicenses(results) {
  const text = fs.readFileSync(LICENSES_PATH, "utf8");
  const start = text.indexOf(LICENSE_HEADER);
  const section = licenseSection(results);
  let next;
  if (start < 0) {
    next = `${text.replace(/\s*$/, "\n")}\n${section}`;
  } else {
    const end = text.indexOf(SEPARATOR, start);
    const rest = end < 0 ? "" : text.slice(end + SEPARATOR.length).replace(/^\s*\n/, "");
    next = text.slice(0, start) + section + rest;
  }
  fs.writeFileSync(LICENSES_PATH, next);
}

const fonts = Object.values(FONT_CATALOGUE).flat();
fs.mkdirSync(FONTS_DIR, { recursive: true });
const results = [];
for (const font of fonts) results.push(await bundleFamily(font));

const css = [
  "/* Generated by frontend/scripts/build_fonts.mjs from src/lib/fonts.ts; do not edit. */",
  "",
  results.map((r) => r.rules.join("\n\n")).join("\n\n"),
  "",
].join("\n");
fs.writeFileSync(path.join(FONTS_DIR, "fonts.css"), css);
writeLicenses(results);

let total = 0;
for (const { font, bytes, files } of results) {
  total += bytes;
  console.log(`${(bytes / 1024).toFixed(0).padStart(6)} kB  ${files} files  ${font.family}`);
}
console.log(`${(total / 1024).toFixed(0).padStart(6)} kB  total`);
