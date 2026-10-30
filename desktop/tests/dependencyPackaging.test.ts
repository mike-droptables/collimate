import { createRequire } from 'node:module';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { createServer } from 'node:http';
import { afterEach, expect, it } from 'vitest';
import { createPackageWithOptions, extractFile, listPackage } from '@electron/asar';
import { downloadArtifact } from '@electron/get';

const require = createRequire(import.meta.url);
const roots: string[] = [];

afterEach(async () => {
  await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true })));
});

it('downloads and checksum-validates artifacts through both installed builder/Electron transports __P5F01', async () => {
  const scratch = path.resolve('../tmp/p5/tmp');
  await mkdir(scratch, { recursive: true });
  const root = await mkdtemp(path.join(scratch, 'dependency-download-'));
  roots.push(root);
  const body = Buffer.from('synthetic Electron artifact bytes');
  const checksum = createHash('sha256').update(body).digest('hex');
  let requests = 0;
  const server = createServer((_request, response) => {
    requests++;
    response.writeHead(200, { 'Content-Length': body.length, 'Cache-Control': 'private, max-age=60' });
    response.end(body);
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('Missing local download address');
    const builderRequire = createRequire(require.resolve('app-builder-lib/package.json'));
    const builderDownload: typeof downloadArtifact = builderRequire('@electron/get').downloadArtifact;
    // The builder's got/cacheable-request path and Electron's undici path both run.
    for (const [name, download] of [['builder', builderDownload], ['electron', downloadArtifact]] as const) {
      const options = {
        version: '43.7.0', artifactName: 'fixture.zip', isGeneric: true as const,
        mirrorOptions: { mirror: `http://127.0.0.1:${address.port}/`, customDir: name },
        checksums: { 'fixture.zip': checksum }, cacheRoot: path.join(root, name), tempDirectory: root,
      };
      const start = requests;
      const file = await download(options);
      expect(await readFile(file)).toEqual(body);
      expect(await download(options)).toBe(file);
      expect(requests - start).toBe(1);
      // Corrupt cache cannot bypass checksum validation; it must fetch a clean artifact.
      await writeFile(file, 'corrupt cached artifact');
      expect(await readFile(await download(options))).toEqual(body);
      expect(requests - start).toBe(2);
    }
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
});

it('packages selected shell assets and validates builder configuration through installed dependencies __P5F01', async () => {
  const scratch = path.resolve('../tmp/p5/tmp');
  await mkdir(scratch, { recursive: true });
  const root = await mkdtemp(path.join(scratch, 'dependency-packaging-'));
  roots.push(root);
  const source = path.join(root, 'app');
  await mkdir(path.join(source, 'dist', 'splash'), { recursive: true });
  await writeFile(path.join(source, 'dist', 'main.cjs'), 'module.exports = "shell-entry";');
  await writeFile(path.join(source, 'dist', 'preload.cjs'), 'module.exports = "shell-preload";');
  await writeFile(path.join(source, 'dist', 'splash', 'index.html'), '<title>Shell splash</title>');
  await writeFile(path.join(source, 'ignored.txt'), 'not selected');
  const archive = path.join(root, 'app.asar');
  // The installed asar glob/minimatch path exercises the patched brace family.
  await createPackageWithOptions(source, archive, { pattern: '/dist/{main,preload}.cjs' });
  expect(listPackage(archive, { isPack: false }).sort()).toEqual(['/dist', '/dist/main.cjs', '/dist/preload.cjs']);
  expect(extractFile(archive, 'dist/main.cjs').toString()).toBe('module.exports = "shell-entry";');
  expect(extractFile(archive, 'dist/preload.cjs').toString()).toBe('module.exports = "shell-preload";');
  // Use the real builder schema validator (Ajv/fast-uri), with both controls.
  const { validateConfiguration } = require('app-builder-lib/out/util/config/config.js');
  const { DebugLogger } = require('builder-util');
  const { load } = require('js-yaml');
  const config = load(await readFile(path.resolve('electron-builder.yml'), 'utf8'));
  await expect(validateConfiguration(config, new DebugLogger(false))).resolves.toBeUndefined();
  await expect(validateConfiguration({ ...config, invalidBuilderOption: true }, new DebugLogger(false))).rejects.toThrow();
});
