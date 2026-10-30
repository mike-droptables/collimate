from __future__ import annotations
import time
import pytest
from collimate._host.verification import run_verifier


def run(body, input=None):
    return run_verifier('var __collimate_verifier={default: function(input){'+body+'}};', input or {}, deadline=time.monotonic()+5)


def test_actual_fresh_engine_normalizes_verdict__D10():
    assert run('return {valid:true,toJSON(){throw Error("untrusted serializer")}}') == {'valid': True}
    assert run('return {valid:false,error:"useful reason"}') == {'valid': False, 'error': 'useful reason'}


@pytest.mark.parametrize('expression', ['null','[]','{}','{valid:1}','{valid:false}','{valid:false,error:42}','Promise.resolve({valid:true})','{valid:true,then(){}}'])
def test_malformed_and_async_verdicts_refuse__D10(expression):
    result=run('return '+expression)
    assert result['valid'] is False
    assert result['error']


def test_context_is_fresh_and_has_no_ambient_authority__D10():
    assert run('globalThis.secret=42; return {valid:true}') == {'valid':True}
    assert run('return {valid:typeof secret==="undefined" && typeof process==="undefined" && typeof fetch==="undefined" && typeof require==="undefined" && typeof __collimate_host_bundle==="undefined"}') == {'valid':True}


def test_input_is_json_data_and_throw_is_invalid__D10():
    assert run('return {valid: input.text === "\\\";throw 42;//"}', {'text':'";throw 42;//'}) == {'valid':True}
    assert run('throw Error("diagnostic")')['valid'] is False


def test_captured_normalizer_survives_intrinsic_mutation__D10():
    assert run('JSON.stringify=()=>"fake"; Array.isArray=()=>true; return {valid:true}') == {'valid':True}


def test_noncallable_default_and_expired_deadline_refuse__D10():
    assert run_verifier('var __collimate_verifier={default:42}', {}, deadline=time.monotonic()+5)['valid'] is False
    assert run_verifier('', {}, deadline=time.monotonic()-1)['valid'] is False


def test_host_bundle_validates_json_only_virtual_inputs__D10():
    result=run('try {__collimate_bundle({"../secret.ts":"secret"},"../secret.ts"); return {valid:true}} catch(e){return {valid:false,error:e.message}}')
    assert result['valid'] is False
    assert 'virtual package file' in result['error']
    result=run('try {__collimate_bundle({"main.ts":{size:4,hash:"x"}},"main.ts"); return {valid:true}} catch(e){return {valid:false,error:e.message}}')
    assert result['valid'] is False
    assert 'opaque' in result['error']


def test_verdict_properties_are_read_once_and_error_is_bounded__D10():
    assert run('let n=0;return {get valid(){return n++ === 0 ? true : "bad"}}') == {'valid':True}
    result=run('return {valid:false,error:"x".repeat(10000)}')
    assert len(result['error']) == 4096


@pytest.mark.parametrize('body', ['while(true){}', 'return {get valid(){while(true){}}}', 'throw {get message(){while(true){}}}'])
def test_hostile_execution_requires_and_obeys_external_watchdog__D10(body):
    """Actual engine loops, killed by a test parent; not an internal timer claim."""
    import os
    import subprocess
    import sys
    script = ('import time; from collimate._host.verification import run_verifier; '
              'run_verifier('+repr('var __collimate_verifier={default: function(){'+body+'}};')+
              ', {}, deadline=time.monotonic()+0.1)')
    child=subprocess.Popen([sys.executable,'-c',script],env=os.environ.copy(),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            child.communicate(timeout=0.3)
    finally:
        child.kill()
        child.communicate(timeout=2)
    assert child.returncode is not None


def test_actual_heap_and_stack_exhaustion_are_invalid__D10():
    assert run('let data=new ArrayBuffer(70*1024*1024); return {valid:true}')['valid'] is False
    assert run('function recurse(){recurse()};recurse();return {valid:true}')['valid'] is False


def test_callback_uses_canonical_native_colon_path_policy__D10_F1280():
    source='export default {__collimate_renderer__:true,component:()=>null}'
    result=run('const compiled=__collimate_bundle(input.files,input.entrypoint);return {valid:typeof compiled.entrypoint_js==="string"}',
               {'files':{'view:main.tsx':source},'entrypoint':'view:main.tsx'})
    assert result == {'valid':True}
