// The SDK's renderer bundle contract is the compiler authority. Refuse drift before build.
import fs from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const contract = JSON.parse(fs.readFileSync(new URL('../../sdk/src/collimate/renderer_bundle_contract.json', import.meta.url), 'utf8'));
const declared = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8')).dependencies['esbuild-wasm'];
const actual = require('esbuild-wasm/package.json').version;
if (declared !== contract.esbuild_version || actual !== contract.esbuild_version) {
  throw new Error(`Browser compiler pin differs from SDK: declared=${declared}, installed=${actual}, SDK=${contract.esbuild_version}`);
}
console.log(`Browser compiler matches canonical SDK ${actual}`);
