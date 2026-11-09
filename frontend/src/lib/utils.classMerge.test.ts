import { describe, expect, it } from 'vitest';
import { cn } from './utils';

describe('component class overrides', () => {
  it('preserves axes and state modifiers while applying caller overrides __F1854', () => {
    expect(cn('px-3 py-2 text-sm hover:bg-red-500', false, ['px-6', { 'hover:bg-blue-500': true }]))
      .toBe('py-2 text-sm px-6 hover:bg-blue-500');
    expect(cn('p-4', 'px-2')).toBe('p-4 px-2');
    expect(cn('px-2', 'p-4')).toBe('p-4');
  });
});
