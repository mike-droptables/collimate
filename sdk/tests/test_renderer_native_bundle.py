"""Real pinned native compiler behavior; no Node runtime in this path."""
import pytest
from collimate._host import renderer_bundle

from tests._workspace import BUILTINS, RENDERERS


def test_native_renderer_compilation_without_node__F1984_F1868(monkeypatch):
    monkeypatch.setenv('PATH', '')
    compile_bundle = getattr(renderer_bundle, 'bundle_renderer', None)
    assert callable(compile_bundle), 'SDK must expose its native pinned compiler'
    result = compile_bundle({'main.ts': b"import {n} from './part'; export default <number>n;", 'part.ts': b'export const n:number=42;'}, entrypoint='main.ts')
    assert '42' in result.code and './part' not in result.code
    assert 'export default' in result.entrypoint_js
    assert len(result.source_hash) == len(result.bundle_hash) == 64


def test_native_verifier_iife_uses_only_canonical_prelude__F1984_F1280():
    compile_verifier = getattr(renderer_bundle, 'compile_verifier', None)
    assert callable(compile_verifier), 'SDK must compile actual verifier modules without Node'
    result = compile_verifier(b"import {ok} from 'collimate/verify'; export default () => ok();", prelude=b'export const ok=()=>({valid:true});')
    assert '__collimate_verifier' in result.code and 'valid: true' in result.code
    with pytest.raises(renderer_bundle.RendererCompileError):
        compile_verifier(b"import x from '/etc/passwd'; export default () => x;", prelude=b'export const ok=1;')


@pytest.mark.parametrize('specifier', ['../outside.ts', '/etc/passwd', 'missing-package', './_runs/private.ts', './missing', 'https:evil'])
def test_native_actual_import_refusal__F1984_P0F21(specifier, tmp_path):
    marker = tmp_path / 'outside.ts'
    marker.write_text('export const PRIVATE_HOST_MARKER=1;')
    with pytest.raises(renderer_bundle.RendererCompileError) as error:
        renderer_bundle.bundle_renderer({'main.ts': f'import value from {specifier!r}; export default value;'.encode()}, entrypoint='main.ts')
    assert 'PRIVATE_HOST_MARKER' not in str(error.value)


def test_native_never_executes_renderer_and_preserves_transform_shape__F1984_F1280(tmp_path):
    marker = tmp_path / 'executed'
    source = f"globalThis.process?.mainModule?.require('fs').writeFileSync({str(marker)!r}, 'bad'); export default defineRenderer({{component:()=>null}});".encode()
    result = renderer_bundle.bundle_renderer({'main.ts': source}, entrypoint='main.ts')
    assert not marker.exists()
    assert 'defineRenderer' in result.entrypoint_js and 'export default' in result.entrypoint_js


@pytest.mark.parametrize('specifier', ['./helper', 'https://example.invalid/code.js', '/api/sdk/renderer.js', 'other'])
def test_verifier_refuses_every_nonprelude_import__F1280_P0F21(specifier):
    with pytest.raises(renderer_bundle.RendererCompileError, match='only collimate/verify'):
        renderer_bundle.compile_verifier(f'import x from {specifier!r}; export default () => x;'.encode(), prelude=b'export const ok=1;')


def test_verifier_canonical_prelude_must_be_self_contained__F1280_P0F21():
    with pytest.raises(renderer_bundle.RendererCompileError, match='only collimate/verify'):
        renderer_bundle.compile_verifier(b"import {ok} from 'collimate/verify'; export default () => ok;", prelude=b"export {ok} from '/etc/passwd';")


def test_verifier_profile_identity_tracks_source_and_prelude__F1868_F1280():
    a = renderer_bundle.verifier_source_hash(b'export default ()=>1;', prelude=b'export const a=1;')
    assert a != renderer_bundle.verifier_source_hash(b'export default ()=>2;', prelude=b'export const a=1;')
    assert a != renderer_bundle.verifier_source_hash(b'export default ()=>1;', prelude=b'export const a=2;')
    result = renderer_bundle.compile_verifier(b'export default ()=>1;', prelude=b'export const a=1;')
    assert result.source_hash == a


def test_native_elapsed_deadline_and_invalid_input_refuse_before_spawn__F1984(monkeypatch):
    import time
    import collimate._esbuild_service as service
    def forbidden(*args, **kwargs):
        pytest.fail('invalid or expired request spawned a compiler')
    monkeypatch.setattr(service.subprocess, 'Popen', forbidden)
    with pytest.raises(renderer_bundle.RendererCompileError, match='deadline'):
        renderer_bundle.bundle_renderer({'main.ts': b'export default 1;'}, entrypoint='main.ts', deadline=time.monotonic()-1)
    for deadline in (float('nan'), 'no'):
        with pytest.raises(renderer_bundle.RendererCompileError, match='deadline'):
            renderer_bundle.compile_verifier(b'export default 1;', prelude=b'', deadline=deadline)
    with pytest.raises(renderer_bundle.RendererCompileError, match='input limits'):
        renderer_bundle.bundle_renderer({'main.ts': 'not bytes'}, entrypoint='main.ts')


def test_native_all_shipped_modules_match_canonical_node_bytes__F1984_F1868__D14(tmp_path):
    import hashlib
    import json
    from pathlib import Path
    import subprocess
    import sys
    out = tmp_path / 'node-bundles'
    subprocess.run([sys.executable, '-m', 'collimate._host.renderer_bundle', '--build', str(RENDERERS), '--builtin', str(BUILTINS), '--out', str(out)], check=True, capture_output=True, timeout=60)
    indexed = {entry['source_hash']: entry for entry in json.loads((out / 'index.json').read_text())['entries']}
    requests = renderer_bundle._build_requests(RENDERERS) + renderer_bundle._build_requests(BUILTINS)
    assert len(requests) == 19
    for request in requests:
        folder = Path(request['directory'])
        files = {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob('*') if p.is_file() and '_runs' not in p.relative_to(folder).parts and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo')}
        native = renderer_bundle.bundle_renderer(files, entrypoint=request['entrypoint'])
        expected = indexed[native.source_hash]
        assert native.code.encode() == (out / expected['file']).read_bytes(), folder.name
        assert native.bundle_hash == expected['bundle_hash'] == hashlib.sha256(native.code.encode()).hexdigest()


def test_actual_native_deadline_kills_and_reaps_compiler__F1984(monkeypatch):
    import time
    from collimate import _esbuild_service as service
    original = service.subprocess.Popen
    started = []
    def record(*args, **kwargs):
        child = original(*args, **kwargs)
        started.append(child)
        return child
    monkeypatch.setattr(service.subprocess, 'Popen', record)
    source = b'\n'.join(f'export const v{i}={i};'.encode() for i in range(200_000))
    with pytest.raises(renderer_bundle.RendererCompileError, match='deadline'):
        renderer_bundle.bundle_renderer({'main.ts': source}, entrypoint='main.ts', deadline=time.monotonic()+.06)
    assert started and all(child.poll() is not None for child in started)


def test_native_ignores_authored_resolution_config_and_keeps_relative_subfolders__F1984_P0F21():
    files = {'nested/main.ts': b"import {n} from '../part'; export default n;", 'part/index.ts': b'export const n=42;',
             'tsconfig.json': b'{"compilerOptions":{"baseUrl":"/","paths":{"*": ["/etc/*"]}}}',
             'package.json': b'{"imports":{"#secret":"/etc/passwd"}}'}
    assert '42' in renderer_bundle.bundle_renderer(files, entrypoint='nested/main.ts').code
    files['nested/main.ts'] = b"import secret from '#secret'; export default secret;"
    with pytest.raises(renderer_bundle.RendererCompileError, match='outside'):
        renderer_bundle.bundle_renderer(files, entrypoint='nested/main.ts')


def test_native_refuses_environment_binary_override__F1984_P0F21(monkeypatch, tmp_path):
    marker = tmp_path / 'override-used'
    override = tmp_path / 'esbuild'
    override.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n')
    override.chmod(0o755)
    monkeypatch.setenv('ESBUILD_BINARY_PATH', str(override))
    monkeypatch.setenv('PATH', str(tmp_path))
    result = renderer_bundle.bundle_renderer({'main.ts': b'export default 1;'}, entrypoint='main.ts')
    assert 'main_default = 1' in result.code and not marker.exists()


def test_actual_build_prefills_accepted_shipped_verifier_same_index__F1868_F1280(tmp_path):
    import json
    import subprocess
    import sys
    from collimate.shared import shared_path
    output = tmp_path / 'bundle-index'
    built = subprocess.run([sys.executable, '-m', 'collimate._host.renderer_bundle', '--build', str(RENDERERS), '--builtin', str(BUILTINS), '--out', str(output)], capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stderr
    source = (BUILTINS / 'definitions/renderer/derived/verify.ts').read_bytes()
    identity = renderer_bundle.verifier_source_hash(source, prelude=shared_path('verify', 'prelude.js').read_bytes())
    indexed = {item['source_hash']: item for item in json.loads((output / 'index.json').read_text())['entries']}
    assert identity in indexed, 'Build must prefill accepted handler verifier in the same bundle index'
    assert '__collimate_verifier' in (output / indexed[identity]['file']).read_text()


def test_build_refuses_stale_accepted_handler_without_replacing_cache__F1868_F1280(tmp_path):
    import shutil
    import subprocess
    import sys
    sources = tmp_path / 'builtin'
    shutil.copytree(BUILTINS, sources)
    out = tmp_path / 'cache'
    command = [sys.executable, '-m', 'collimate._host.renderer_bundle', '--build', str(RENDERERS), '--builtin', str(sources), '--out', str(out)]
    first = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stderr
    previous = (out / 'index.json').read_bytes()
    verifier = sources / 'definitions/renderer/derived/verify.ts'
    verifier.write_bytes(verifier.read_bytes() + b'\n// unaccepted change\n')
    second = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert second.returncode != 0 and 'matching accepted derived' in second.stderr
    assert (out / 'index.json').read_bytes() == previous
