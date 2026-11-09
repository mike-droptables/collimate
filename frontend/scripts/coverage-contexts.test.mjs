import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import './vitest-config.test.mjs';

// Check the real report after Vitest, so accidentally excluding the entire
// directory cannot turn its threshold into a vacuous pass.
test('F1919 coverage reports every context module and executed context code', () => {
  const root = new URL('../', import.meta.url);
  const contexts = readdirSync(new URL('src/contexts/', root))
    .filter(name => /\.[jt]sx?$/.test(name) && !/\.(test|spec)\./.test(name));
  assert.ok(contexts.length > 0);
  const records = readFileSync(new URL('coverage/lcov.info', root), 'utf8').split('end_of_record');
  let executed = 0;
  for (const name of contexts) {
    const relative = `src/contexts/${name}`;
    const absolute = fileURLToPath(new URL(relative, root));
    const record = records.find(block => block.split('\n')
      .some(line => line === `SF:${relative}` || line === `SF:${absolute}`));
    assert.ok(record, `${relative} is missing from actual coverage`);
    executed += Number(record.match(/^LH:(\d+)$/m)?.[1] ?? 0);
  }
  assert.ok(executed > 0, 'context coverage must include executed lines');
});

test('F1919 coverage reports mounted StackApp and executed application lines', () => {
  const root = new URL('../', import.meta.url);
  const relative = 'src/components/StackApp.tsx';
  const absolute = fileURLToPath(new URL(relative, root));
  const records = readFileSync(new URL('coverage/lcov.info', root), 'utf8').split('end_of_record');
  const record = records.find(block => block.split('\n')
    .some(line => line === `SF:${relative}` || line === `SF:${absolute}`));
  assert.ok(record, `${relative} is missing from actual coverage`);
  assert.ok(Number(record.match(/^LH:(\d+)$/m)?.[1] ?? 0) > 0,
    'StackApp coverage must include executed lines from its mounted tests');
});
