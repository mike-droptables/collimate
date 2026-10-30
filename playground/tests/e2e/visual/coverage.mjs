/** Fail before mounting if registered fixtures cannot yield the promised captures. */
export function visualCoverage(catalog, fixtures, expectedCount) {
  const ids = Object.keys(fixtures).sort();
  const natives = catalog.filter(r => r.embed === 'native').map(r => r.id).sort();
  if (expectedCount < 1 || ids.length !== expectedCount) {
    throw new Error(`Expected ${expectedCount} nonzero visual fixtures, found ${ids.length}`);
  }
  const missing = ids.filter(id => !natives.includes(id));
  if (missing.length) throw new Error(`Visual fixtures absent from native catalog: ${missing.join(', ')}`);
  return { covered: ids, advertised: natives, uncovered: natives.filter(id => !ids.includes(id)) };
}
export function completedCoverage(coverage, completed) {
  if (JSON.stringify([...completed].sort()) !== JSON.stringify(coverage.covered)) {
    throw new Error(`Incomplete screenshot assertions: ${completed.join(', ')}`);
  }
}
