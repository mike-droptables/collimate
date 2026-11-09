// SPDX-License-Identifier: MIT
// Pinned production compiler. Only the provided package map is readable by
// esbuild; URL imports remain external and no renderer code is executed.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import esbuild from 'esbuild';
import {createRendererCompiler, packagePath} from './renderer_bundle_core.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const contractPath = [path.join(here, '../renderer_bundle_contract.json'),
  path.join(here, '../src/collimate/renderer_bundle_contract.json')].find(p => fs.existsSync(p));
if (!contractPath) throw new Error('SDK renderer compilation contract is missing');
export const CONTRACT = JSON.parse(fs.readFileSync(contractPath, 'utf8'));
export function compiler() {
  if (esbuild.version !== CONTRACT.esbuild_version) {
    throw new Error(`esbuild ${esbuild.version} differs from SDK pin ${CONTRACT.esbuild_version}; run npm ci in ${here}`);
  }
  return esbuild;
}
const portable = createRendererCompiler({contract: CONTRACT, compiler: compiler(),
  digest: bytes => new Uint8Array(crypto.createHash('sha256').update(bytes).digest())});
export const rendererSourceHash = portable.rendererSourceHash;
export const bundleRenderer = portable.bundleRenderer;

export function collectPackage(directory) {
  const files = new Map();
  if (fs.lstatSync(directory).isSymbolicLink()) throw new Error("Symlink outside the renderer's folder is not allowed");
  function walk(dir, prefix = '') {
    for (const item of fs.readdirSync(dir, {withFileTypes: true})) {
      if (item.name === '_runs' || item.name === '__pycache__' || /\.py[co]$/.test(item.name)) continue;
      const name = packagePath(prefix + item.name), absolute = path.join(dir, item.name);
      if (item.isSymbolicLink()) throw new Error(`Symlink outside the renderer's folder is not allowed: ${name}`);
      if (item.isDirectory()) walk(absolute, name + '/');
      else if (item.isFile()) files.set(name, fs.readFileSync(absolute));
      else throw new Error(`Not a regular renderer file: ${name}`);
    }
  }
  walk(directory);
  return files;
}
async function main() {
  const args = process.argv.slice(2);
  if (args.length !== 2 || args[0] !== '--out') throw new Error('usage: renderer_bundle.mjs --out <directory> (JSON requests on stdin)');
  const requests = JSON.parse(fs.readFileSync(0, 'utf8'));
  if (!Array.isArray(requests) || !requests.length) throw new Error('No renderer build requests');
  const built = [];
  for (const request of requests) built.push(await bundleRenderer({
    files: collectPackage(request.directory), entrypoint: request.entrypoint,
  }));
  const output = path.resolve(args[1]);
  fs.mkdirSync(output, {recursive: true});
  const entries = [];
  const seen = new Map();
  for (const bundle of built) {
    if (seen.has(bundle.source_hash)) {
      if (seen.get(bundle.source_hash) !== bundle.bundle_hash) throw new Error("Conflicting bundle identity");
      continue;
    }
    seen.set(bundle.source_hash, bundle.bundle_hash);
    const file = bundle.bundle_hash + '.js';
    const temp = path.join(output, `.${file}.${process.pid}.tmp`);
    fs.writeFileSync(temp, bundle.code); fs.renameSync(temp, path.join(output, file));
    entries.push({source_hash: bundle.source_hash, bundle_hash: bundle.bundle_hash, file});
  }
  entries.sort((a, b) => a.source_hash.localeCompare(b.source_hash));
  const temp = path.join(output, `.index.${process.pid}.tmp`);
  fs.writeFileSync(temp, JSON.stringify({schema: 1, compiler: CONTRACT.esbuild_version, entries}, null, 2) + '\n');
  fs.renameSync(temp, path.join(output, 'index.json'));
  console.log(`Built ${entries.length} renderer bundles with esbuild ${CONTRACT.esbuild_version}`);
}
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => { console.error(error); process.exitCode = 1; });
}
