import { fileFixture } from '@/test/fileFixtures';
import {describe,expect,it} from 'vitest';
import {definitionText,parseDefinition,replaceFiles,textFile} from './definitionFiles';
describe('definition YAML authoring',()=>{
 it('roundtrips real schema fields and unknown metadata without scalar coercion__D13',()=>{
  const text='id: writer\nname: Draft\nmain_config: settings.set_yaml\nunknown:\n  date: 2026-10-03\n  enabled: true\n  unset: null\n  count: 0\n  word: yes\nfields:\n  - key: title\n    type: text\n    default: Hello\n';
  const value=parseDefinition(text);expect(value.unknown).toEqual({date:'2026-10-03',enabled:true,unset:null,count:0,word:'yes'});
  expect(parseDefinition(definitionText({...value,name:'Edited'}))).toEqual({...value,name:'Edited'});
 });
 it('preserves anchored metadata values through a form save__D13',()=>{
  const value=parseDefinition('id: anchored\nfirst: &shared\n  keys: [one, two]\nsecond: *shared\n');
  expect(value.first).toEqual(value.second);expect(parseDefinition(definitionText(value))).toEqual(value);
 });
 it.each(['name: [','[]','null','id: one\nid: two','loop: &loop\n  child: *loop'])('refuses malformed or non-mapping definitions__D13 (%s)',text=>expect(()=>parseDefinition(text)).toThrow());
 it('preserves unrelated binary and text metadata during definition updates__D15',()=>{
  const binary=fileFixture('binary.bin','',{encoding:'binary',body_hash:'hash'});
  const husk=fileFixture('large.txt','',{body_hash:'large'});
  const next=replaceFiles([binary,husk],[textFile('view/view.renderer','id: view')]);
  expect(next[0]).toBe(binary);expect(next[1]).toBe(husk);
 });
});
