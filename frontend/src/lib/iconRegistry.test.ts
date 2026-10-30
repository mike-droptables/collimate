import { describe, expect, it } from 'vitest';
import { ICON_REGISTRY, resolveIcon } from './iconRegistry';

describe('resolveIcon', () => {
  it('resolves a known key case-insensitively', () => {
    expect(resolveIcon('Clipboard')).toBe(ICON_REGISTRY.clipboard);
  });

  it('returns null for unset and unknown keys', () => {
    expect(resolveIcon('')).toBeNull();
    expect(resolveIcon(null)).toBeNull();
    expect(resolveIcon('no-such-icon')).toBeNull();
  });

  it('does not resolve Object.prototype members', () => {
    expect(resolveIcon('constructor')).toBeNull();
    expect(resolveIcon('__proto__')).toBeNull();
    expect(resolveIcon('hasOwnProperty'.toLowerCase())).toBeNull();
  });
});
