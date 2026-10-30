import { test } from 'node:test';
import assert from 'node:assert/strict';
import { visualCoverage, completedCoverage } from './coverage.mjs';
const catalog = [{ id: 'text', embed: 'native' }, { id: 'frame', embed: 'iframe' }];
test('P4F46 retired/empty/iframe fixtures cannot vacuously pass', () => {
  assert.throws(() => visualCoverage(catalog, { markdown: '' }, 1), /absent/);
  assert.throws(() => visualCoverage(catalog, {}, 1), /nonzero/);
  assert.throws(() => visualCoverage(catalog, {}, 0), /nonzero/);
  assert.throws(() => visualCoverage(catalog, { frame: '' }, 1), /absent/);
});
test('P4F46 completed assertions must match coverage including no duplicates', () => {
  const coverage = visualCoverage(catalog, { text: '' }, 1);
  assert.throws(() => completedCoverage(coverage, []), /Incomplete/);
  assert.throws(() => completedCoverage(coverage, ['text', 'text']), /Incomplete/);
  completedCoverage(coverage, ['text']);
  assert.deepEqual(coverage.uncovered, []);
});
