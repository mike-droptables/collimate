import { describe, it, expect } from 'vitest';
import { getTheme, softVariant } from './themes';
import {
  copyUserTheme, derivedSoft, hexOf, newUserTheme, normalizeUserThemes, uniqueLabel, userThemeToDef,
} from './userThemes';

describe('user skins', () => {
  it('a new user skin has no overrides and looks exactly like its base (S02)', () => {
    const user = newUserTheme('night-cool', 'Mine');
    expect(user).toMatchObject({ label: 'Mine', scheme: 'dark', vars: {}, baseThemeId: 'night-cool' });
    expect(user.id).toMatch(/^user\.[a-z0-9]{8}$/);
    expect(userThemeToDef(user).vars).toEqual(getTheme('night-cool').vars);
  });

  it('a copy keeps base, scheme and overrides under a new id (S02)', () => {
    const source = { ...newUserTheme('paper', 'A'), vars: { ink: '#101010' } };
    const copy = copyUserTheme(source, 'B');
    expect(copy.id).not.toBe(source.id);
    expect(copy).toMatchObject({ label: 'B', baseThemeId: 'paper', scheme: 'light', vars: { ink: '#101010' } });
    expect(copy.vars).not.toBe(source.vars);
  });

  it('an overridden accent brings its soft fill at the base alpha until the soft is set (S02)', () => {
    const base = getTheme('night').vars;
    const user = { ...newUserTheme('night', 'A'), vars: { brass: '#204060' } };
    expect(derivedSoft(user, 'brassSoft')).toBe(softVariant('#204060', base.brassSoft));
    expect(userThemeToDef(user).vars.brassSoft).toBe('rgba(32, 64, 96, 0.16)');
    expect(userThemeToDef(user).vars.tealSoft).toBe(base.tealSoft);
    const explicit = { ...user, vars: { ...user.vars, brassSoft: 'rgba(1, 2, 3, 0.5)' } };
    expect(userThemeToDef(explicit).vars.brassSoft).toBe('rgba(1, 2, 3, 0.5)');
    const rgbAccent = { ...user, vars: { red: 'rgb(16, 32, 48)' } };
    expect(userThemeToDef(rgbAccent).vars.redSoft).toBe('rgba(16, 32, 48, 0.16)');
  });

  it('hexOf reads hex and rgb colours and refuses the rest (S02)', () => {
    expect(hexOf('#ABCDEF')).toBe('#abcdef');
    expect(hexOf('#abc')).toBe('#aabbcc');
    expect(hexOf('#11223344')).toBe('#112233');
    expect(hexOf('rgba(198, 157, 100, 0.16)')).toBe('#c69d64');
    expect(hexOf('rgb(1 2 3 / 50%)')).toBe('#010203');
    expect(hexOf('rgb(300, 0, 0)')).toBeUndefined();
    expect(hexOf('teal')).toBeUndefined();
  });

  it('uniqueLabel numbers a taken label (S02)', () => {
    expect(uniqueLabel('My theme', [])).toBe('My theme');
    expect(uniqueLabel('My theme', ['My theme', 'My theme 2'])).toBe('My theme 3');
  });

  it('normalises stored user skins: a built-in base, a scheme, and string skin tokens only (S02)', () => {
    expect(normalizeUserThemes(undefined)).toEqual([]);
    expect(normalizeUserThemes({})).toEqual([]);
    const stored = [
      { id: 'user.a', label: 'A', baseThemeId: 'gone-light', scheme: 'light', createdAt: 5,
        vars: { surface: '#000', backdrop: 'data:x', ink: '#101010', sheet: 3 } },
      { id: 'user.b', baseThemeId: 'gone', vars: null },
      { id: 'user.c', baseThemeId: 'paper', scheme: 'sepia', label: 'C', createdAt: 1, vars: {} },
      { id: 'night', label: 'not a user skin', vars: {} },
      null,
    ];
    expect(normalizeUserThemes(stored)).toEqual([
      { id: 'user.a', label: 'A', baseThemeId: 'paper', scheme: 'light', createdAt: 5, vars: { ink: '#101010' } },
      { id: 'user.b', label: '', baseThemeId: 'night', scheme: 'dark', createdAt: 0, vars: {} },
      { id: 'user.c', label: 'C', baseThemeId: 'paper', scheme: 'light', createdAt: 1, vars: {} },
    ]);
  });
});
