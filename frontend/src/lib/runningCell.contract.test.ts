import { expect, it } from 'vitest';
import fixture from '../../../sdk/shared/run-path-contract.json';
import { isRunPath, normalizeRunPath, runCardPath, runLabelOf } from './runningCell';

it.each(fixture.paths)('run path $path follows the shared host fixture (F1238, F1884)', row => {
  expect(normalizeRunPath(row.path)).toBe(row.normalized);
  expect(isRunPath(row.path)).toBe(row.is_run_path);
  expect(runLabelOf(row.path)).toBe(row.label);
});

it.each(fixture.labels)('run card for $label follows the shared fixture (F1884)', row => {
  expect(runCardPath({ folder: `_runs/${row.label}`, label: row.label })).toBe(row.card);
});
