import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { loadConfigFromFile, resolveConfig } from 'vite';

const root = fileURLToPath(new URL('../', import.meta.url));
async function resolved(name) {
  const loaded = await loadConfigFromFile({ command: 'serve', mode: 'test' }, `${root}${name}`, root);
  assert.ok(loaded, `${name} must load`);
  return resolveConfig({ ...loaded.config, root, configFile: false }, 'serve', 'test');
}

test('F1093 Vitest resolves inherited application transforms and settings with isolated fixture routing', async () => {
  const application = await resolved('vite.config.ts');
  const tests = await resolved('vitest.config.ts');
  assert.equal(tests.worker.format, application.worker.format);
  assert.deepEqual(tests.build.target, application.build.target);
  assert.equal(tests.build.chunkSizeWarningLimit, application.build.chunkSizeWarningLimit);
  assert.equal(tests.rawBase, application.rawBase);
  assert.deepEqual(tests.resolve.alias.find(alias => alias.find === '@'),
    application.resolve.alias.find(alias => alias.find === '@'));
  for (const plugin of application.plugins.filter(plugin => plugin.name.startsWith('vite:react'))) {
    assert.equal(tests.plugins.filter(candidate => candidate.name === plugin.name).length, 1,
      `${plugin.name} must be inherited exactly once`);
  }
  assert.ok(tests.plugins.some(plugin => plugin.name === 'collimate-renderer-fixtures'));
  assert.equal(tests.server.proxy, undefined, 'fixture server must not forward API requests externally');
  assert.deepEqual(tests.test.projects.map(project => project.test.name), ['node', 'jsdom']);
});
