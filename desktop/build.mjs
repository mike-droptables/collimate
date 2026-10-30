// Bundles the shell. Two entry points, both CommonJS — Electron's main
// process and preload scripts are loaded as CJS, and bundling keeps the
// packaged app to a handful of files with no node_modules to ship.
//
// `node build.mjs` builds once; `node build.mjs --watch` rebuilds on save.
import { context, build } from 'esbuild';
import { cp, mkdir, readFile, rm, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const outdir = path.join(root, 'dist');
const watch = process.argv.includes('--watch');

const pkg = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'));

// Ceiling on the main bundle, checked after every full build.
//
// electron-updater and its closure (builder-util-runtime, js-yaml, semver,
// fs-extra, lodash bits) are bundled in, which took main.cjs from ~61 KB to
// ~650 KB in one commit. That's fine — it's a rounding error next to the
// Electron runtime it ships beside — but it happened silently, and the next
// dependency that drags a tree in should not. This number is a tripwire,
// not a budget: if a deliberate addition crosses it, raise it in the same
// commit and say what grew.
const MAX_MAIN_BUNDLE_BYTES = 900_000;

/** @type {import('esbuild').BuildOptions} */
const common = {
  bundle: true,
  platform: 'node',
  format: 'cjs',
  // Electron 45 ships Node 24 (process.versions.node); move this with
  // Electron's embedded Node when Electron is bumped.
  target: 'node24',
  // Provided by the runtime, never bundled.
  //
  // electron-updater is deliberately NOT here: it's a devDependency that
  // gets inlined into main.cjs like everything else, because its closure is
  // plain CJS with no native code and no runtime file lookups relative to
  // its own package dir. The `await import('electron-updater')` in
  // main/updater.ts survives that — esbuild keeps it a dynamic import of an
  // inlined chunk, so the deb/dev paths still never evaluate it.
  //
  // If bundling it ever misbehaves (a future release adding an .node
  // binding, or a require() of a file it expects beside itself): move it to
  // `dependencies` in package.json and add it here. electron-builder copies
  // production node_modules into the app on its own, independently of the
  // `files:` globs in electron-builder.yml, so it will still ship — the
  // bundle just stops carrying it.
  external: ['electron'],
  // The shell's own version, baked in — see SHELL_VERSION in shared/bridge.ts.
  define: { __SHELL_VERSION__: JSON.stringify(pkg.version) },
  sourcemap: true,
  logLevel: 'info',
};

async function copyAssets() {
  // The splash is plain HTML — copied, not bundled, so what ships is
  // exactly what's in src/splash.
  await cp(path.join(root, 'src', 'splash'), path.join(outdir, 'splash'), { recursive: true });
  // Window/taskbar icon, referenced as path.join(__dirname, 'app-icon.png')
  // by the main + splash BrowserWindows. Lives in dist/ so it resolves the
  // same way in a dev run and inside the packaged asar.
  await cp(path.join(root, 'icons', 'app-icon.png'), path.join(outdir, 'app-icon.png'));
}

async function run() {
  await rm(outdir, { recursive: true, force: true });
  await mkdir(outdir, { recursive: true });

  const targets = [
    { ...common, entryPoints: [path.join(root, 'src', 'main', 'index.ts')], outfile: path.join(outdir, 'main.cjs') },
    {
      ...common,
      entryPoints: [path.join(root, 'src', 'preload', 'index.ts')],
      outfile: path.join(outdir, 'preload.cjs'),
    },
  ];

  if (watch) {
    const contexts = await Promise.all(targets.map((options) => context(options)));
    await Promise.all(contexts.map((ctx) => ctx.watch()));
    await copyAssets();
    console.log('watching for changes…');
    return;
  }

  await Promise.all(targets.map((options) => build(options)));
  await copyAssets();
  await checkBundleSize();
}

async function checkBundleSize() {
  const main = path.join(outdir, 'main.cjs');
  const { size } = await stat(main);
  const kb = (size / 1024).toFixed(0);
  if (size > MAX_MAIN_BUNDLE_BYTES) {
    throw new Error(
      `dist/main.cjs is ${kb} KB, over the ${(MAX_MAIN_BUNDLE_BYTES / 1024).toFixed(0)} KB ceiling ` +
        'in build.mjs. Something new is being bundled — check it belongs in the main process, ' +
        'then raise MAX_MAIN_BUNDLE_BYTES with a note about what grew.',
    );
  }
  console.log(`  dist/main.cjs ${kb} KB`);
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
