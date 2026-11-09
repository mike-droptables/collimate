from __future__ import annotations
import json
import time
from pathlib import Path
import pytest
from collimate._host.renderer_bundle import compile_verifier
from collimate._host.verification import run_verifier
from collimate.options_stack import _glob

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='module')
def code():
    source=b'''import {parseYaml,parseJson,validateSchema,tokenizeHtml,matchGlob} from "collimate/verify";
export default function(input) {
 try {
  if(input.action==="glob") return {valid:matchGlob(input.path,input.pattern,input.root)===input.expected};
  if(input.action==="yaml") return {valid:parseYaml(input.text).name==="quoted: value"};
  if(input.action==="duplicate") {parseYaml(input.text);return {valid:false,error:"duplicate key accepted"};}
  if(input.action==="html") {const tokens=tokenizeHtml(input.text);return {valid:tokens.some(t=>t.type==="open" && t.name==="div" && t.attributes.id==="anchor") && tokens.filter(t=>t.type==="text").map(t=>t.text).join("")==="A & B"};}
  if(input.action==="schema") return validateSchema({type:"object",required:["name"],additionalProperties:false,properties:{name:{type:"string"}}},parseJson(input.text));
  return {valid:false,error:"unknown action"};
 }catch(error){return {valid:false,error:error.message};}
}'''
    return compile_verifier(source,prelude=(ROOT/'shared/verify/prelude.js').read_bytes(),deadline=time.monotonic()+5).code


def invoke(code, **input):
    return run_verifier(code,input,deadline=time.monotonic()+5)


@pytest.mark.parametrize('root,pattern,path',[
 ('a[1]','**','a[1]/one/two.txt'),('a*','*.txt','a*/note.txt'),('a?','**/*.txt','a?/note.txt'),
 ('a','../brief.md','brief.md'),('', '*.md','sub/brief.md'),('', '**/*.md','brief.md'),
 ('','a**b','a/x/b'),('', '?[1].txt','a[1].txt'),('', '[ab].txt','a.txt'),
])
def test_actual_prelude_glob_dialect_matches_options_python__D10(root,pattern,path,code):
    expected=bool(_glob(root,pattern).fullmatch(path))
    assert invoke(code,action='glob',root=root,pattern=pattern,path=path,expected=expected)=={'valid':True}


def test_actual_prelude_yaml_json_schema_and_html__D10(code):
    assert invoke(code,action='yaml',text='name: "quoted: value"')=={'valid':True}
    assert invoke(code,action='duplicate',text='name: one\nname: two')['valid'] is False
    assert 'duplicate key accepted' not in invoke(code,action='duplicate',text='name: one\nname: two')['error']
    assert invoke(code,action='html',text='<DIV id="anchor">A &amp; B</DIV>')=={'valid':True}
    assert invoke(code,action='schema',text='{"name":"ok"}')=={'valid':True}
    assert invoke(code,action='schema',text='{"name":42}')['valid'] is False
    assert invoke(code,action='schema',text='{"name":"ok","unexpected":1}')['valid'] is False


def test_canonical_prelude_build_is_current_and_self_contained__D10_D12(tmp_path):
    import subprocess
    result=subprocess.run(['node',str(ROOT/'scripts/build_verify_prelude.mjs'),'--check'],capture_output=True,text=True,timeout=20,cwd=tmp_path)
    assert result.returncode==0, result.stderr
    assert not any(line.startswith('import ') for line in (ROOT/'shared/verify/prelude.js').read_text().splitlines())
    licenses=(ROOT/'shared/verify/dependency-licenses.txt').read_text()
    assert all(f'=== {pin} ===' in licenses for pin in ('yaml@2.9.1','ajv@8.20.0','htmlparser2@12.0.0'))


def test_prelude_manifest_pins_its_sources_and_its_built_bytes(tmp_path, monkeypatch):
    """prelude.json, committed beside the git-ignored prelude.js, pins both:
    an edited source or built byte is refused, and the installed prelude
    verifies with no Node on PATH."""
    import shutil
    from collimate.renderer_runtime import SourceChanged, verify_prelude_resources
    shutil.copytree(ROOT/'shared/verify', tmp_path/'shared/verify')
    for name in ('scripts/build_verify_prelude.mjs','scripts/package.json','scripts/package-lock.json',
                 'src/collimate/renderer_bundle_contract.json'):
        (tmp_path/name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, tmp_path/name)
    assert set(verify_prelude_resources(tmp_path)) == {'prelude.js','dependency-licenses.txt'}
    source = tmp_path/'shared/verify/prelude.ts'
    original = source.read_bytes()
    source.write_bytes(original + b'\n// edited\n')
    with pytest.raises(SourceChanged, match='source changed'):
        verify_prelude_resources(tmp_path)
    source.write_bytes(original)
    (tmp_path/'shared/verify/prelude.js').write_bytes(b'wrong compiled bytes')
    with pytest.raises(ValueError, match='resource mismatch: prelude.js'):
        verify_prelude_resources(tmp_path)
    (tmp_path/'shared/verify/prelude.js').unlink()
    with pytest.raises(FileNotFoundError):
        verify_prelude_resources(tmp_path)
    monkeypatch.setenv('PATH', '')
    assert verify_prelude_resources()['prelude.js'] == (ROOT/'shared/verify/prelude.js').read_bytes()


@pytest.mark.parametrize('html,tokens',[
 ('<iframe><b>x</b></iframe>',[['open','iframe'],['text','<b>x</b>'],['close','iframe']]),
 ('<textarea>a &amp; b</textarea>',[['open','textarea'],['text','a & b'],['close','textarea']]),
 ('<h1>a<h2>b</h2>',[['open','h1'],['text','a'],['close','h1'],['open','h2'],['text','b'],['close','h2']]),
 ('<a href=1>x<a href=2>y</a>',[['open','a'],['text','x'],['close','a'],['open','a'],['text','y'],['close','a']]),
 ('<?php x ?><!x>',[['comment','?php x ?'],['comment','x']]),
 ('<!--a--!>b</>',[['comment','a'],['text','b']]),
 ('<svg><foreignObject/></svg>',[['open','svg'],['open','foreignObject'],['close','foreignObject'],['close','svg']]),
])
def test_actual_prelude_html_tokens_follow_whatwg_tokenisation(html,tokens):
    source = b'''import {tokenizeHtml} from 'collimate/verify';
export default input => {
 const out=[];
 for(const t of tokenizeHtml(input.text)){const value=t.type==='text'||t.type==='comment'?t.text:t.name;const last=out[out.length-1];
  if(t.type==='text'&&last&&last[0]==='text') last[1]+=value; else out.push([t.type,value]);}
 return JSON.stringify(out)===input.expected ? {valid:true} : {valid:false,error:JSON.stringify(out)};
};'''
    compiled = compile_verifier(source, prelude=(ROOT/'shared/verify/prelude.js').read_bytes()).code
    assert invoke(compiled, text=html, expected=json.dumps(tokens,separators=(',',':'))) == {'valid': True}


def test_deep_yaml_has_parser_error_boundary_in_actual_engine__P3F03():
    source = b'''import {parseYaml} from 'collimate/verify';
export default input => {
 try {parseYaml(input.text);return {valid:false,error:'deep input accepted'};}
 catch(error){return error.name === 'YAMLParseError' ? {valid:true} : {valid:false,error:error.name};}
};'''
    compiled = compile_verifier(source, prelude=(ROOT/'shared/verify/prelude.js').read_bytes()).code
    assert invoke(compiled, text='['*1500+'0'+']'*1500) == {'valid': True}
