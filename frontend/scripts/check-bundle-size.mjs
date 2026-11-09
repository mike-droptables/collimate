#!/usr/bin/env node
/**
 * Bundle-size budget.
 *
 * Reads the just-built `dist/assets/*.js` files, sums their gzipped size,
 * and fails if:
 *   - the sum exceeds MAX_GZIP_KB (hard ceiling), OR
 *   - the sum exceeds the baseline stored in `.bundle-baseline.json`
 *     by more than GROWTH_PCT (elastic ceiling tracking history)
 *
 * Usage:
 *   node scripts/check-bundle-size.mjs          # check
 *   node scripts/check-bundle-size.mjs --update # rewrite baseline
 *
 * Baseline moves require a landed feature's measured cost in its ledger row.
 * A failed gate alone never authorizes changing the baseline (guide 6.5).
 */
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ASSETS = path.join(ROOT, "dist", "assets");
const BASELINE_PATH = path.join(ROOT, ".bundle-baseline.json");

const MAX_GZIP_KB = 800;
const GROWTH_PCT = 10;

const args = new Set(process.argv.slice(2));
const update = args.has("--update");

if (!fs.existsSync(ASSETS)) {
  console.error(`error: no dist/assets — run \`npm run build\` first`);
  process.exit(2);
}

const files = fs.readdirSync(ASSETS).filter((f) => f.endsWith(".js"));
if (files.length === 0) {
  console.error("error: dist/assets has no .js files");
  process.exit(2);
}

let total = 0;
const per = [];
for (const f of files) {
  const buf = fs.readFileSync(path.join(ASSETS, f));
  const gz = zlib.gzipSync(buf, { level: 9 }).length;
  total += gz;
  per.push({ file: f, rawKb: +(buf.length / 1024).toFixed(1), gzKb: +(gz / 1024).toFixed(1) });
}
per.sort((a, b) => b.gzKb - a.gzKb);

const totalKb = +(total / 1024).toFixed(1);
console.log(`bundle size (gzip): ${totalKb} kB across ${files.length} chunks`);
for (const entry of per.slice(0, 10)) {
  console.log(`  ${entry.gzKb.toFixed(1).padStart(7)} kB   ${entry.file}`);
}
if (per.length > 10) console.log(`  …(+${per.length - 10} smaller chunks)`);

if (totalKb > MAX_GZIP_KB) {
  console.error(
    `FAIL: total gzip size ${totalKb} kB exceeds hard ceiling ${MAX_GZIP_KB} kB`,
  );
  process.exit(1);
}

if (update) {
  fs.writeFileSync(
    BASELINE_PATH,
    JSON.stringify({ totalGzipKb: totalKb, generatedAt: new Date().toISOString() }, null, 2) + "\n",
  );
  console.log(`baseline written: ${totalKb} kB`);
  process.exit(0);
}



if (fs.existsSync(BASELINE_PATH)) {
  const { totalGzipKb: baseline } = JSON.parse(fs.readFileSync(BASELINE_PATH, "utf8"));
  const growth = ((totalKb - baseline) / baseline) * 100;
  console.log(`baseline: ${baseline} kB   growth: ${growth.toFixed(1)}%`);
  if (growth > GROWTH_PCT) {
    console.error(
      `FAIL: bundle grew ${growth.toFixed(1)}% over baseline ${baseline} kB ` +
        `(ceiling ${GROWTH_PCT}%). Baseline changes require a landed feature's measured cost in its ledger row; a failing gate alone is not sufficient.`,
    );
    process.exit(1);
  }
} else {
  console.warn(
    "warn: no .bundle-baseline.json found. Run `npm run bundle:update` to seed.",
  );
}

console.log("OK");
