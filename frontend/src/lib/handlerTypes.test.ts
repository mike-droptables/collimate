import {expect, it} from 'vitest';
import {handlerForPath, handlerTypeRules} from './handlerTypes';

it('returns the original whole handler for case-folded suffixes __D09_D15_P3F11', () => {
  const handler = {id: 'german', cell_id: 'target', definition_path: 'german/german.handler', types: [{ext: '.STRASSE'}]};
  expect(handlerForPath([handler], 'Folder/sample.STRAẞE')).toBe(handler);
  expect(handlerForPath([handler], 'sample.unknown')).toBeNull();
});

it('disables only the clashing type and preserves its longer winning rule __D09_D11_D15_P3F11', () => {
  const multi = {types: [{ext: '.long.FOO', render_as: '.HTML'}, {ext: '.bar', render_as: '.MD'}], clashes: ['.LONG.foo']};
  const short = {types: [{ext: '.foo', render_as: '.txt'}]};
  expect(handlerForPath([multi, short], 'sample.long.foo')).toBeNull();
  expect(handlerForPath([multi, short], 'sample.bar')).toBe(multi);
  expect(handlerTypeRules([multi, short])).toEqual([
    {ext: '.long.foo', handler: null, renderAs: null},
    {ext: '.bar', handler: multi, renderAs: '.md'},
    {ext: '.foo', handler: short, renderAs: '.txt'},
  ]);
});

it('counts folded type collisions while excluding whole disabled records __D09_D11_D15_P3F11', () => {
  const first = {types: [{ext: '.straße', render_as: '.html'}]};
  const second = {types: [{ext: '.STRASSE', render_as: '.txt'}]};
  expect(handlerForPath([first, second], 'sample.strasse')).toBeNull();
  expect(handlerForPath([first, {...second, disabled: true}], 'sample.strasse')).toBe(first);
  expect(handlerTypeRules([first, second])[0].renderAs).toBeNull();
});

it('selects a longest plain type without borrowing a shorter alias __D09_D11_D15_P3F11', () => {
  const short = {types: [{ext: '.foo', render_as: '.html'}]};
  const long = {types: [{ext: '.long.foo'}]};
  expect(handlerForPath([short, long], 'sample.long.foo')).toBe(long);
  expect(handlerTypeRules([short, long])[0].renderAs).toBeNull();
});
