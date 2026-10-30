import { describe, expect, it } from 'vitest';
import { errorMessage } from '../src/main/errors.js';

describe('caught shell diagnostics __F1021', () => {
  it.each([
    [new Error('download failed'), 'download failed'],
    ['server refused', 'server refused'],
    [null, 'null'],
    [{ reason: 'lost' }, '[object Object]'],
  ])('preserves existing diagnostic conversion for %s __F1021', (value, expected) => {
    expect(errorMessage(value)).toBe(expected);
  });
});
