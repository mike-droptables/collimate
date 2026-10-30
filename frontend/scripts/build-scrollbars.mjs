// The sandbox document's copy of the workbench scrollbars: src/lib/scrollbars.ts
// bundled from scrollbars.document.ts into public/scrollbars.js, which
// sandbox.html loads. One implementation, served twice. `--check` refuses
// drift between the source and the committed bundle (a vitest does the same).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const OUTPUT = path.join(root, 'public/scrollbars.js');
const BANNER = '// Generated from src/lib/scrollbars.ts by scripts/build-scrollbars.mjs; do not edit.\n';

export async function buildScrollbars() {
  const result = await build({
    absWorkingDir: root, entryPoints: ['src/lib/scrollbars.document.ts'], bundle: true, write: false,
    format: 'esm', platform: 'browser', target: 'es2022', legalComments: 'none',
  });
  return BANNER + result.outputFiles[0].text;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const code = await buildScrollbars();
  if (process.argv.includes('--check')) {
    const current = fs.existsSync(OUTPUT) ? fs.readFileSync(OUTPUT, 'utf8') : '';
    if (current !== code) { console.error('public/scrollbars.js differs from src/lib/scrollbars.ts: run node scripts/build-scrollbars.mjs'); process.exit(1); }
    console.log('public/scrollbars.js matches src/lib/scrollbars.ts');
  } else {
    fs.writeFileSync(OUTPUT, code);
    console.log(`wrote ${path.relative(root, OUTPUT)} (${code.length} bytes)`);
  }
}
