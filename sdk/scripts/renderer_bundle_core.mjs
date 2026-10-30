// SPDX-License-Identifier: MIT
// Portable renderer compiler policy. Files, compiler and SHA-256 are supplied
// by the host; this module has no filesystem, network or Node dependency.
const encoder = new TextEncoder();
const decoder = new TextDecoder();

export function packagePath(value) {
  if (typeof value !== 'string' || !value || /[\\\x00]/.test(value) || /^[A-Za-z]:/.test(value) ||
      value.split('/').some(p => ['', '.', '..', '_runs'].includes(p)) ||
      decoder.decode(encoder.encode(value)) !== value) {
    throw new Error(`Invalid renderer package path (outside the renderer's folder or _runs): ${JSON.stringify(value)}`);
  }
  return value;
}
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  }
  return value;
}
function compareBytes(a, b) {
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return a.length - b.length;
}
const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
function snapshotFiles(files) {
  if (!(files instanceof Map)) throw new Error('Renderer files must be a Map');
  const snapshot = new Map();
  for (const [name, bytes] of files) {
    packagePath(name);
    if (!(bytes instanceof Uint8Array)) throw new Error(`Renderer file must contain bytes: ${name}`);
    snapshot.set(name, new Uint8Array(bytes));
  }
  return snapshot;
}
function extension(name) {
  const base = name.slice(name.lastIndexOf('/') + 1);
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(dot) : '';
}
function relativePath(importer, spec) {
  const parts = importer.split('/').slice(0, -1);
  for (const part of spec.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..' && parts.length && parts[parts.length - 1] !== '..') parts.pop();
    else parts.push(part);
  }
  return packagePath(parts.join('/'));
}

export function createRendererCompiler({contract, compiler, digest}) {
  if (!contract || contract.schema !== 1 || compiler.version !== contract.esbuild_version) {
    throw new Error(`Compiler version ${compiler?.version} differs from SDK pin ${contract?.esbuild_version}`);
  }
  // Caller state cannot change the flags halfway through an asynchronous build.
  const policy = JSON.parse(JSON.stringify(contract));
  async function sha(bytes) {
    const result = await digest(bytes);
    if (!(result instanceof Uint8Array) || result.length !== 32) throw new Error('SHA-256 must return 32 bytes');
    return new Uint8Array(result);
  }
  async function hashFiles(files, entrypoint) {
    packagePath(entrypoint);
    if (!files.has(entrypoint)) throw new Error('Renderer entrypoint is absent from the package');
    const names = [...files.keys()].map(name => [name, encoder.encode(name)]);
    names.sort((a, b) => compareBytes(a[1], b[1]));
    const chunks = [encoder.encode('collimate-renderer-bundle-v1'),
      encoder.encode(JSON.stringify(canonical(policy))), encoder.encode(entrypoint)];
    const digests = await Promise.all(names.map(([name]) => sha(files.get(name))));
    names.forEach(([, nameBytes], i) => chunks.push(nameBytes, digests[i]));
    const size = chunks.reduce((sum, chunk) => sum + 4 + chunk.length, 0);
    const framed = new Uint8Array(size);
    const view = new DataView(framed.buffer);
    let offset = 0;
    for (const chunk of chunks) {
      view.setUint32(offset, chunk.length, false);
      framed.set(chunk, offset + 4);
      offset += chunk.length + 4;
    }
    return hex(await sha(framed));
  }
  function packagePlugin(files, entrypoint) {
    function resolve(spec, importer) {
      if (!spec.startsWith('./') && !spec.startsWith('../')) {
        throw new Error(`import ${JSON.stringify(spec)} is not relative`);
      }
      if (spec.includes('\\') || spec.includes('\0') || spec.split('/').includes('_runs')) {
        throw new Error(`import ${JSON.stringify(spec)} is outside the renderer's folder or _runs`);
      }
      const joined = relativePath(importer, spec);
      const candidates = [joined, ...policy.resolve_extensions.map(e => joined + e),
        ...policy.resolve_extensions.map(e => joined + '/index' + e)];
      const found = candidates.find(candidate => files.has(candidate));
      if (!found) throw new Error(`Cannot resolve ${JSON.stringify(spec)} inside the renderer's folder`);
      return found;
    }
    return {name: 'collimate-package', setup(build) {
      build.onResolve({filter: /.*/}, args => {
        try {
          if (args.kind === 'entry-point') return {path: packagePath(entrypoint), namespace: 'collimate-package'};
          if (policy.external_prefixes.some(prefix => args.path.startsWith(prefix))) {
            return {path: args.path, external: true};
          }
          return {path: resolve(args.path, args.importer), namespace: 'collimate-package'};
        } catch (error) { return {errors: [{text: error.message}]}; }
      });
      build.onLoad({filter: /.*/, namespace: 'collimate-package'}, args => {
        const contents = files.get(args.path);
        if (contents === undefined) return {errors: [{text: `Missing renderer file ${args.path}`}]};
        const loader = policy.options.loader[extension(args.path)];
        if (!loader) return {errors: [{text: `Unsupported renderer module type: ${args.path}`}]};
        return {contents, loader};
      });
    }};
  }
  async function rendererSourceHash(files, entrypoint) {
    return hashFiles(snapshotFiles(files), entrypoint);
  }
  async function bundleRenderer({files, entrypoint}) {
    const snapshot = snapshotFiles(files);
    const source_hash = await hashFiles(snapshot, entrypoint);
    const result = await compiler.build({...policy.options, entryPoints: [entrypoint],
      write: false, logLevel: 'silent', plugins: [packagePlugin(snapshot, entrypoint)]});
    if (result.outputFiles?.length !== 1) throw new Error('A renderer must produce exactly one module');
    const code = result.outputFiles[0].text;
    return {source_hash, code, bundle_hash: hex(await sha(encoder.encode(code)))};
  }
  return {rendererSourceHash, bundleRenderer};
}
