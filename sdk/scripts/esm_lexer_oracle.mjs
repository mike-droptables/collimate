#!/usr/bin/env node
// SPDX-License-Identifier: MIT
//
// esm_lexer_oracle.mjs: the test oracle for src/collimate/_host/esm_lexer.py, which
// ports es-module-lexer's import scan. This runs es-module-lexer 3.0.4 itself
// (a devDependency of scripts/package.json), through its minimal build, the
// v2-shaped records `n/s/e/d`, over the inputs a test names.
//
// Usage: node scripts/esm_lexer_oracle.mjs <inputs.json>
//   inputs.json: {"files": [path, ...]} or {"sources": [string, ...]}
// Prints one JSON line per input, in order:
//   {"imports": [[n, s, e, d], ...]}   n is null for a computed import()
//   {"error": idx}                      es-module-lexer's parse error offset
// Offsets are converted from UTF-16 units to code points, the unit of the
// Python port. Nothing here runs in the product: the mirror never uses Node.
import { readFileSync } from 'node:fs';
import { parse } from 'es-module-lexer/minimal';

const inputs = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const sources = inputs.files ? inputs.files.map(file => readFileSync(file, 'utf8')) : inputs.sources;

function codePointOffsets(source) {
  if (!/[\uD800-\uDFFF]/.test(source)) return null;
  const map = new Int32Array(source.length + 1);
  let point = 0;
  for (let unit = 0; unit < source.length; unit++) {
    map[unit] = point;
    const code = source.charCodeAt(unit);
    if (code >= 0xD800 && code <= 0xDBFF && unit + 1 < source.length) {
      const next = source.charCodeAt(unit + 1);
      if (next >= 0xDC00 && next <= 0xDFFF) { map[++unit] = point + 1; }
    }
    point++;
  }
  map[source.length] = point;
  return map;
}

const lines = [];
for (const source of sources) {
  const map = codePointOffsets(source);
  // An error offset can lie past the end (the lexer reads beyond it).
  const at = offset => (!map || offset < 0 ? offset
    : offset <= source.length ? map[offset] : map[source.length] + offset - source.length);
  try {
    const [imports] = parse(source);
    lines.push(JSON.stringify({ imports: imports.map(item => [item.n ?? null, at(item.s), at(item.e), item.d >= 0 ? at(item.d) : item.d]) }));
  } catch (error) {
    lines.push(JSON.stringify({ error: at(error.idx ?? -1) }));
  }
}
process.stdout.write(lines.join('\n') + '\n');
