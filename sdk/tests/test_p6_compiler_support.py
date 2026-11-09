"""P6F01: compiler path and verifier protocol support at their actual public owners."""
import hashlib
import json

import pytest

from collimate._host.renderer_bundle import validate_path
from collimate._host.verification import VERIFICATION_BOUNDARY


@pytest.mark.parametrize('value', [
    None, 42, '', '/root.ts', 'C:main.ts', 'z:/main.ts', 'a\\b.ts',
    'a\x00b.ts', 'a//b.ts', './main.ts', '../main.ts', 'a/../b.ts',
    '_runs/main.ts', 'a/_runs/main.ts', 'main.ts/',
])
def test_compiler_path_refuses_unsafe_virtual_names__P6F01(value):
    with pytest.raises(ValueError, match='Invalid renderer package path'):
        validate_path(value)


@pytest.mark.parametrize('value', ['main.tsx', 'view:main.tsx', 'nested/view.ts', '\u03bb/\U00010000.ts'])
def test_compiler_path_preserves_exact_valid_names__P6F01(value):
    assert validate_path(value) == value


def test_verification_boundary_preserves_exact_protocol_bytes__P6F01():
    assert hashlib.sha256(VERIFICATION_BOUNDARY.encode()).hexdigest() == (
        'be0f7b931bd39f4c1de30ee75ae0cd435705c6c4d59dee97d52cf8514f887b03')


def test_actual_verification_boundary_owns_callback_and_verdict_serialization__P6F01():
    from collimate._host.verifier_engine import context_type

    context = context_type()()
    calls = []

    def host(files, entrypoint):
        calls.append((json.loads(files), json.loads(entrypoint)))
        return json.dumps({'ok': True, 'result': {'entrypoint_js': 'compiled'}})

    context.add_callable('__collimate_host_bundle', host)
    normalize = context.eval(VERIFICATION_BOUNDARY)
    assert context.eval('typeof __collimate_host_bundle') == 'undefined'
    context.eval('''globalThis.__collimate_verifier={default(input){
      const result=__collimate_bundle(input.files,input.entrypoint);
      JSON.stringify=()=>"untrusted";
      return {valid:result.entrypoint_js==="compiled",toJSON(){throw Error("untrusted")}};
    }}''')
    files = {'view:main.tsx': 'export default "\u03bb"'}
    assert json.loads(normalize(json.dumps({'files': files, 'entrypoint': 'view:main.tsx'}))) == {'valid': True}
    assert calls == [(files, 'view:main.tsx')]
    context.eval('globalThis.__collimate_verifier={default(){return {valid:false,error:"x".repeat(5000)}}}')
    assert json.loads(normalize('{}')) == {'valid': False, 'error': 'x' * 4096}


def test_compiler_support_uses_actual_owners_without_generator_exports__P6F01():
    import collimate
    from collimate import native
    from collimate._host import renderer_bundle, verification
    import gen

    assert not hasattr(renderer_bundle, '_path')
    assert not hasattr(verification, '_BOUNDARY')
    for name in ('validate_path', 'VERIFICATION_BOUNDARY'):
        assert name not in native.__all__
        assert name not in collimate.__all__
        assert not hasattr(gen, name)
