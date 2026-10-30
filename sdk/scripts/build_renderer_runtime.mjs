#!/usr/bin/env node
// Build canonical SDK runtime modules; authored renderer compilation is separate.
//
//   node scripts/build_renderer_runtime.mjs            write the outputs, the manifest and the codec descriptor
//   node scripts/build_renderer_runtime.mjs --outputs  write the outputs only (scripts/build_outputs.py)
//   node scripts/build_renderer_runtime.mjs --check    rebuild and compare: the manifest, the descriptor
//                                                      and any output already built
//
// The outputs (react.js, session.js, common.json, snapshot-codecs.json in
// shared/renderer-runtime) are git-ignored; the committed manifest beside them,
// index.json, pins their sha256 and their sources'.
import { build, version } from 'esbuild';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { outputs as codecFiles, snapshotCodecs } from './build_snapshot_codecs.mjs';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const mode = process.argv.includes('--check') ? 'check' : process.argv.includes('--outputs') ? 'outputs' : 'write';
const contract = JSON.parse(await readFile(path.join(root, 'src/collimate/renderer_bundle_contract.json'), 'utf8'));
if (version !== contract.esbuild_version) throw Error('Runtime compiler differs from canonical pin');
// The runtime's React is the toolchain contract's, built from what packages/renderer-hooks installs.
const toolchain = JSON.parse(await readFile(path.join(root, 'shared/contracts/toolchain.json'), 'utf8'));
const pins = {'react': `https://esm.sh/react@${toolchain.react}?target=es2022`,
  'react-dom/client': `https://esm.sh/react-dom@${toolchain.react_dom}/client?deps=react@${toolchain.react}&target=es2022`};
const output = path.join(root, 'shared/renderer-runtime');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const codecs = await snapshotCodecs();
const built = {};
const common = {};
const reactSource = await readFile(path.join(root, 'packages/renderer-hooks/node_modules/react/cjs/react.production.js'), 'utf8');
for (const [name, pinned] of [['react', toolchain.react], ['react-dom', toolchain.react_dom]]) {
  const installed = JSON.parse(await readFile(path.join(root, 'packages/renderer-hooks/node_modules', name, 'package.json'), 'utf8')).version;
  if (installed !== pinned) throw Error(`packages/renderer-hooks has ${name} ${installed} installed; the toolchain contract says ${pinned}: npm ci there`);
}
const reactNames = [...new Set([...reactSource.matchAll(/exports\.([A-Za-z_$][\w$]*)/g)].map(match => match[1]))];
const reactExports = `export const {${reactNames.join(',')}}=React;export default React;`;
for (const [specifier, source] of Object.entries(pins)) {
  const contents = specifier === 'react'
    ? `import React from 'react';${reactExports}`
    : `export {createRoot,hydrateRoot,version} from 'react-dom/client';`;
  const result = await build({
    absWorkingDir: path.join(root, 'packages/renderer-hooks'),
    stdin: {contents, resolveDir: path.join(root, 'packages/renderer-hooks')},
    write: false, bundle: true, minify: true, define: {'process.env.NODE_ENV': '\"production\"'},
    format: 'esm', platform: 'browser', target: 'es2022', legalComments: 'none',
    plugins: [{name: 'shared-react', setup(builder) {
      if (specifier === 'react') return;
      builder.onResolve({filter: /^react$/}, () => ({path: 'shared-react', namespace: 'prepared'}));
      builder.onLoad({filter: /.*/, namespace: 'prepared'}, () => ({contents: `import React from ${JSON.stringify(pins.react)};${reactExports}`}));
      builder.onResolve({filter: /^https:\/\//}, args => ({path: args.path, external: true}));
    }}]
  });
  const body = new TextDecoder().decode(result.outputFiles[0].contents);
  common[source] = {body, hash: hash(body)};
}
common[`https://esm.sh/react@${toolchain.react}/es2022/react.mjs`] = common[pins.react];
for (const [name, source] of [['react.js', 'runtime.tsx'], ['session.js', 'session.ts']]) {
  const result = await build({absWorkingDir: root, entryPoints: [path.join(root, 'packages/renderer-hooks/src', source)],
    outfile: path.join(output, name), write: false, bundle: true, format: 'esm', platform: 'browser', target: 'es2022',
    legalComments: 'none', tsconfigRaw: {compilerOptions: {jsx: 'react'}},
    plugins: [{name: 'runtime-react-identity', setup(builder) {
      builder.onResolve({filter: /^(react|react-dom\/client)$/}, args => ({path: pins[args.path], external: true}));
    }}]});
  const bytes = Buffer.from(result.outputFiles[0].contents);
  if (name === 'session.js' && /^import\s/m.test(bytes.toString('utf8'))) throw Error('Session runtime imports a dependency');
  built[name] = bytes;
}
built['common.json'] = Buffer.from(JSON.stringify(common) + '\n');
built['snapshot-codecs.json'] = Buffer.from(codecs.manifest);
const inputs = ['scripts/build_renderer_runtime.mjs', 'src/collimate/renderer_bundle_contract.json', 'shared/contracts/toolchain.json',
  'scripts/build_snapshot_codecs.mjs', 'scripts/snapshot-codecs/milkdown.ts', 'scripts/package.json', 'scripts/package-lock.json',
  'scripts/snapshot-codecs/embed-markdown.ts', 'scripts/snapshot-codecs/snapshot-model.ts',
  ...['color.ts','document-view.tsx','index.tsx','media.tsx','mirror.ts','platform.tsx','runtime.tsx','session.ts','style-control.tsx','syntax.ts','types.ts'].map(name => 'packages/renderer-hooks/src/' + name)];
const manifest = {schema: 1, compiler: version, inputs: {}, outputs: {}};
for (const input of inputs.sort()) manifest.inputs[input] = hash(await readFile(path.join(root,input)));
for (const name of ['react.js','session.js','common.json','snapshot-codecs.json']) manifest.outputs[name] = hash(built[name]);
// Committed beside the sources: the manifest and the milkdown renderer's codec descriptor.
const committed = {[path.join(output, 'index.json')]: JSON.stringify(manifest,null,2) + '\n', [codecFiles.descriptor]: codecs.descriptor};
const existing = file => readFile(file).catch(error => { if (error.code === 'ENOENT') return null; throw error; });
if (mode === 'check') {
  const differ = [];
  for (const [file, text] of Object.entries(committed))
    if (!Buffer.from(text).equals(await existing(file) ?? Buffer.alloc(0))) differ.push(path.relative(root, file));
  if (differ.length) throw Error('Generated runtime differs from canonical source: ' + differ.join(', '));
  const stale = [];
  for (const [name, bytes] of Object.entries(built)) {
    const on = await existing(path.join(output, name));
    if (on && !on.equals(bytes)) stale.push(name);
  }
  if (stale.length) throw Error('Built runtime outputs differ from their sources (rebuild them: node scripts/build_renderer_runtime.mjs --outputs): ' + stale.join(', '));
} else {
  await mkdir(output, {recursive: true});
  for (const [name, bytes] of Object.entries(built)) await writeFile(path.join(output, name), bytes);
  if (mode === 'write') for (const [file, text] of Object.entries(committed)) await writeFile(file, text);
}
