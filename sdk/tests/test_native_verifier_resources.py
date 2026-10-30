"""P4F05: prepared private engine integrity and actual execution, without optional skips."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import time
import types
import zipfile

import pytest

from collimate._host import verifier_engine as engine
from collimate._host.verification import run_verifier

from tests._workspace import GENERATORS


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_verifier_engine', ROOT / 'scripts/prepare_verifier_engine.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def test_private_binding_ignores_global_quickjs_and_keeps_actual_limits__P4F05(monkeypatch):
    poisoned = types.ModuleType('quickjs')
    poisoned.Context = lambda: pytest.fail('external binding used')
    monkeypatch.setitem(sys.modules, 'quickjs', poisoned)
    body = 'var __collimate_verifier={default:()=>({valid:true})};'
    assert run_verifier(body, {}, deadline=time.monotonic()+5) == {'valid': True}
    context = engine.context_type()()
    assert context.eval('1+2') == 3
    assert sys.modules['collimate._quickjs'].Context is engine.context_type()
    assert Path(sys.modules['collimate._quickjs'].__file__).resolve() == (engine.payload_directory(engine.native_target()) / '_quickjs.abi3.so').resolve()
    assert sys.modules['quickjs'] is poisoned


def test_owned_payload_rejects_corruption_and_rewritten_manifest__P4F05(tmp_path):
    target = engine.native_target()
    original = engine.payload_directory(target)
    payload = tmp_path / 'payload'
    shutil.copytree(original, payload)
    assert engine.verify_payload(payload, target)['target'] == target
    binary = payload / '_quickjs.abi3.so'
    binary.write_bytes(binary.read_bytes()+b'changed')
    with pytest.raises(ValueError, match='integrity'):
        engine.verify_payload(payload, target)
    manifest = payload / 'manifest.json'
    record = json.loads(manifest.read_text())
    record['files']['_quickjs.abi3.so'] = hashlib.sha256(binary.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(record))
    # The three upstream binaries are independently pinned in the source contract.
    if 'extension_sha256' in engine.contract()['targets'][target]:
        with pytest.raises(ValueError, match='pinned source'):
            engine.verify_payload(payload, target)
    shutil.rmtree(payload)
    shutil.copytree(original, payload)
    license = payload / 'LICENSE.quickjs-ng'
    license.unlink()
    license.symlink_to(original / 'LICENSE.quickjs-ng')
    with pytest.raises(ValueError, match='integrity'):
        engine.verify_payload(payload, target)


def test_missing_payload_never_invokes_compiler_or_download__P4F05(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, '__file__', str(tmp_path / 'src/collimate/_host/verifier_engine.py'))
    with pytest.raises(RuntimeError, match='Prepare the SDK private verifier'):
        engine.payload_directory(engine.native_target())


def test_native_target_has_no_unsupported_source_fallback__P4F05(monkeypatch):
    monkeypatch.setattr(engine.platform, 'machine', lambda: 'unknown')
    with pytest.raises(RuntimeError, match='no prepared binding'):
        engine.native_target()


def wheel(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            archive.writestr(info, data)
    return output.getvalue()


@pytest.mark.parametrize('entries', [[], [('_quickjs.abi3.so', b'a', 0o100644)]*2,
    [('_quickjs.abi3.so', b'target', 0o120777)]])
def test_pinned_wheel_selection_rejects_missing_duplicate_or_symlink__P4F05(entries):
    with pytest.raises(ValueError):
        prepare.wheel_extension(wheel(entries))


def test_wheel_selection_does_not_extract_unrelated_paths__P4F05():
    raw = wheel([('_quickjs.abi3.so', b'engine', 0o100644), ('../../outside', b'bad', 0o100644)])
    assert prepare.wheel_extension(raw) == b'engine'


@pytest.mark.parametrize('name, kind', [('quickjs_ng-0.17.0.1/../../outside', 'regular'),
    ('/outside', 'regular'), ('quickjs_ng-0.17.0.1/link', 'symlink')])
def test_pinned_source_extraction_refuses_unsafe_members__P4F05(name, kind):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        info = tarfile.TarInfo(name)
        if kind == 'symlink':
            info.type = tarfile.SYMTYPE
            info.linkname = '/outside'
            archive.addfile(info)
        else:
            info.size = 1
            archive.addfile(info, io.BytesIO(b'a'))
    with pytest.raises(ValueError):
        prepare.source_members(output.getvalue())


def test_archive_hash_precedes_any_unpack_or_compile__P4F05(tmp_path):
    path = tmp_path / 'bad.whl'
    path.write_bytes(b'wrong')
    with pytest.raises(ValueError, match='hash or size'):
        prepare.fetch({'filename': path.name, 'sha256': '0'*64}, tmp_path, True)


def test_intel_mac_has_explicit_build_and_install_never_compiles__P4F05(tmp_path, monkeypatch):
    locked = engine.contract()
    assert set(locked['targets']) == {'linux-x64', 'linux-arm64', 'darwin-x64', 'darwin-arm64'}
    assert locked['targets']['darwin-x64']['source_build'] is True
    monkeypatch.setattr(prepare, 'fetch', lambda *a: pytest.fail('implicit download/build'))
    with pytest.raises(FileNotFoundError):
        prepare.prepare('darwin-x64', tmp_path, tmp_path)


def test_actual_first_party_wheel_provisions_fresh_source_payload_and_refuses_mismatch__P4F05(tmp_path):
    target = engine.native_target()
    original = engine.payload_directory(target)
    archive = tmp_path / 'sdk.whl'
    with zipfile.ZipFile(archive, 'w') as wheel:
        for name in ('manifest.json', '_quickjs.abi3.so', 'LICENSE.quickjs-ng', 'LICENSE.quickjs-engine'):
            wheel.writestr('collimate/native_verifier/'+target+'/'+name, (original / name).read_bytes())
    out = tmp_path / 'fresh-source/build/native_verifier'
    record = prepare.provision_sdk_wheel(archive, target, out)
    assert engine.verify_payload(out / target, target) == record
    other = next(name for name in engine.contract()['targets'] if name != target)
    with pytest.raises(ValueError):
        prepare.provision_sdk_wheel(archive, other, out)
    assert not (out / other).exists()
    assert engine.verify_payload(out / target, target) == record
    # A first-party archive cannot relabel a different source pin.
    invalid = tmp_path / 'wrong-source.whl'
    with zipfile.ZipFile(archive) as before, zipfile.ZipFile(invalid, 'w') as after:
        for name in before.namelist():
            raw = before.read(name)
            if name.endswith('manifest.json'):
                changed = json.loads(raw)
                changed['source_sha256'] = '0'*64
                raw = json.dumps(changed).encode()
            after.writestr(name,raw)
    with pytest.raises(ValueError, match='pinned contract'):
        prepare.provision_sdk_wheel(invalid, target, out)
    assert engine.verify_payload(out / target, target) == record


def test_native_binary_cannot_be_relabelled_as_another_architecture__P4F05():
    target = engine.native_target()
    raw = (engine.payload_directory(target) / '_quickjs.abi3.so').read_bytes()
    engine.verify_binary_target(raw, target)
    for other in set(engine.contract()['targets']) - {target}:
        with pytest.raises(ValueError, match='architecture'):
            engine.verify_binary_target(raw, other)


def test_fresh_source_core_import_and_provisioned_context_need_no_installed_sdk__P4F05(tmp_path):
    import subprocess
    source = tmp_path / 'checkout'
    for name in ('collimate', 'gen'):
        shutil.copytree(ROOT / 'src' / name, source / 'src' / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    target = engine.native_target()
    # Use the same explicit payload seam as a clean source checkout.
    original = engine.payload_directory(target)
    archive = tmp_path / 'prepared-sdk.whl'
    with zipfile.ZipFile(archive, 'w') as wheel:
        for name in ('manifest.json', '_quickjs.abi3.so', 'LICENSE.quickjs-ng', 'LICENSE.quickjs-engine'):
            wheel.writestr('collimate/native_verifier/'+target+'/'+name, (original / name).read_bytes())
    prepare.provision_sdk_wheel(archive, target, source / 'src/collimate/native_verifier')
    worker = '''
import importlib.metadata,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import collimate
from collimate._host.verifier_engine import context_type,payload_directory,native_target
try:importlib.metadata.distribution('collimate-sdk')
except importlib.metadata.PackageNotFoundError:pass
else:raise AssertionError('ambient SDK distribution')
assert 'pydantic' not in sys.modules and 'quickjs' not in sys.modules
assert context_type()().eval('40+2')==42
assert str(Path(sys.argv[1]).parent) in str(payload_directory(native_target()))
print('source-only private engine, no ambient SDK/global engine/compiler')
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-c', worker, str(source / 'src')],
                            cwd=tmp_path, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'source-only private engine' in result.stdout


def test_actual_base_context_preserves_private_native_payload_for_source_runtime__P4F05(tmp_path, monkeypatch):
    """The launch image's real staging seam copies the resources runtime selects."""
    import subprocess
    monkeypatch.syspath_prepend(str(GENERATORS))
    from _env_shared import base_context
    from collimate import native
    from collimate.testing import make_test_bridge
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    bridge = make_test_bridge(workspace)
    native.set_bridge(bridge)
    try:
        staged = Path(base_context()) / 'sdk'
        target = engine.native_target()
        original = engine.payload_directory(target)
        for path in original.iterdir():
            assert (staged / 'collimate/native_verifier' / target / path.name).read_bytes() == path.read_bytes()
        worker = '''
import importlib.util,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
assert importlib.util.find_spec('quickjs') is None
from collimate._host.verifier_engine import context_type,native_target,payload_directory
assert context_type()().eval('6*7')==42
assert payload_directory(native_target()).resolve().is_relative_to(Path(sys.argv[1]).resolve())
print('actual staged private payload executed')
'''
        result = subprocess.run([sys.executable, '-I', '-S', '-c', worker, str(staged)],
                                text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert 'actual staged private payload executed' in result.stdout
    finally:
        native._clear_bridge()
        bridge.close()


def test_wheel_host_tag_requires_both_linux_container_payloads__P4F05(tmp_path):
    """An installed package must retain container resources, independent of host tag."""
    spec = importlib.util.spec_from_file_location('check_native_wheels', ROOT / 'scripts/check_native_wheels.py')
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    target = engine.native_target()
    platform = engine.contract()['targets'][target]['platform_tag']
    path = tmp_path / f'collimate_sdk-0.2.0-cp312-abi3-{platform}.whl'
    def archive(targets, extra=(), omit=()):
        with zipfile.ZipFile(path, 'w') as wheel:
            wheel.writestr('collimate_sdk-0.2.0.dist-info/METADATA', 'Name: collimate-sdk\nVersion: 0.2.0\nRequires-Dist: pyyaml>=6\n')
            tags = ''.join(f'Tag: cp312-abi3-{tag}\n' for tag in platform.split('.'))
            wheel.writestr('collimate_sdk-0.2.0.dist-info/WHEEL', 'Root-Is-Purelib: false\n' + tags)
            for selected in targets:
                original = engine.payload_directory(selected)
                for name in ('manifest.json', '_quickjs.abi3.so', 'LICENSE.quickjs-ng', 'LICENSE.quickjs-engine'):
                    wheel.writestr('collimate/native_verifier/'+selected+'/'+name, (original / name).read_bytes())
            # Other resource presence belongs to the existing packaging controls.
            for name in ('collimate/__init__.py', 'collimate/_host/packaging/present', 'collimate/shared/present', 'collimate/renderer_compilers/manifest.json',
                         'collimate/renderer_compilers/linux-x64/esbuild', *check.BUILT_SHARED,
                         'collimate/renderer_bundle_contract.json',
                         'collimate/renderer_tools/renderer_bundle.mjs', 'gen/__init__.py', *extra):
                if name not in omit:
                    wheel.writestr(name, b'present')
    archive({target})
    with pytest.raises(ValueError, match='both Linux container payloads'):
        check.check_wheel(path)
    archive(engine.wheel_payload_targets(target))
    assert check.check_wheel(path) == (target, '0.2.0')
    # The built, git-ignored runtime and prelude ship in every wheel.
    for built in ('collimate/shared/verify/prelude.js', 'collimate/shared/renderer-runtime/common.json'):
        archive(engine.wheel_payload_targets(target), omit=(built,))
        with pytest.raises(ValueError, match=f'Wheel missing {built}'):
            check.check_wheel(path)
    # The first-party content trees ship in the app's wheel, never the SDK's.
    for tree in ('generators', 'renderers', 'builtin', 'suggested'):
        archive(engine.wheel_payload_targets(target), extra=(f'collimate/{tree}/present',))
        with pytest.raises(ValueError, match=f'carries the app content collimate/{tree}/'):
            check.check_wheel(path)
    for missing in ('linux-x64', 'linux-arm64'):
        archive(engine.wheel_payload_targets(target) - {missing})
        with pytest.raises(ValueError, match='both Linux container payloads'):
            check.check_wheel(path)
    assert engine.wheel_payload_targets('darwin-x64') == {'darwin-x64', 'linux-x64', 'linux-arm64'}
    assert engine.wheel_payload_targets('darwin-arm64') == {'darwin-arm64', 'linux-x64', 'linux-arm64'}


def source_completion_case(tmp_path):
    """Small local archive exercises provenance/closure without a network dependency."""
    import copy
    locked = copy.deepcopy(engine.contract())
    pinned = locked['source_build']['upstream']
    prefix = 'quickjs-' + pinned['revision']
    includes = ''.join(f'#include "{name}"\n' for name in pinned['headers']).encode()
    members = {'quickjs_ng-0.17.0.1/upstream-quickjs/quickjs.c': includes}
    upstream = {'quickjs.c': includes}
    for name in pinned['headers']:
        raw = f'/* fixture header {name} */\n'.encode()
        upstream[name] = raw
        pinned['headers'][name] = hashlib.sha256(raw).hexdigest()
    atomics = upstream['quickjs-c-atomics.h']
    pinned['license_notice'] = atomics[:atomics.index(b'*/') + 2].decode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, raw in upstream.items():
            info = tarfile.TarInfo(prefix + '/' + name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    raw = output.getvalue()
    pinned['sha256'] = hashlib.sha256(raw).hexdigest()
    (tmp_path / pinned['filename']).write_bytes(raw)
    return members, locked


def test_explicit_source_completion_adds_only_pinned_omitted_headers__P4F05(tmp_path):
    members, locked = source_completion_case(tmp_path)
    result = prepare.complete_intel_source(members, locked, tmp_path, True)
    assert all(result[name] == raw for name, raw in members.items())
    assert set(result) - set(members) == {
        'quickjs_ng-0.17.0.1/upstream-quickjs/' + name for name in locked['source_build']['upstream']['headers']}
    assert set(engine.contract()['source_build']['upstream']['headers']) == {
        'quickjs-c-atomics.h', 'builtin-array-fromasync.h', 'builtin-iterator-zip-keyed.h', 'builtin-iterator-zip.h'}


@pytest.mark.parametrize('failure', ['archive', 'overlap', 'header', 'include', 'notice'])
def test_source_completion_refuses_unpinned_or_incomplete_inputs__P4F05(tmp_path, failure):
    members, locked = source_completion_case(tmp_path)
    pinned = locked['source_build']['upstream']
    if failure == 'archive':
        pinned['sha256'] = '0'*64
    elif failure == 'notice':
        pinned['license_notice'] = 'unrelated notice'
    elif failure == 'header':
        pinned['headers']['quickjs-c-atomics.h'] = '0'*64
    elif failure == 'overlap':
        members['quickjs_ng-0.17.0.1/upstream-quickjs/quickjs.c'] += b'changed'
    else:
        members['quickjs_ng-0.17.0.1/module.c'] = b'#include "missing.h"\n'
    before = dict(members)
    with pytest.raises(ValueError):
        prepare.complete_intel_source(members, locked, tmp_path, True)
    assert members == before


@pytest.mark.parametrize('binary_kind, minimum', [('x64', '11.0'), ('arm64', '11.0'), ('fat', '11.0'), ('x64', '12.0'), ('x64', 'missing'), ('x64', 'ambiguous'), ('x64', 'wrong-platform')])
@pytest.mark.parametrize('minimum_command', ['LC_BUILD_VERSION', 'LC_VERSION_MIN_MACOSX'])
def test_intel_source_build_selects_honest_platform_and_checks_binary__P4F05(monkeypatch, binary_kind, minimum, minimum_command):
    """A universal2 interpreter default must not mislabel a thin Intel build."""
    import struct
    locked = engine.contract()
    monkeypatch.setattr(prepare, 'native_target', lambda: 'darwin-x64')
    monkeypatch.setattr(prepare.importlib.metadata, 'version', lambda name: locked['source_build'][name])
    seen = {}
    raw = bytearray(32)
    raw[:4] = b'\xcf\xfa\xed\xfe' if binary_kind != 'fat' else b'\xca\xfe\xba\xbe'
    struct.pack_into('<I', raw, 4, 0x01000007 if binary_kind == 'x64' else 0x0100000C)
    def build(command, *, cwd, env, check):
        seen.update(command=command, env=env)
        # Reproduce the actual python.org runner's universal2 default; only
        # the supported explicit wheel option changes its output platform.
        platform = next((arg.split('=', 1)[1] for arg in command if arg.startswith('--plat-name=')),
                        'macosx_11_0_universal2')
        dist = cwd / 'dist'
        dist.mkdir()
        (dist / f'quickjs_ng-0.17.0.1-cp310-abi3-{platform}.whl').write_bytes(
            wheel([('_quickjs.abi3.so', bytes(raw), 0o100644)]))
    monkeypatch.setattr(prepare.subprocess, 'run', build)
    members = {'quickjs_ng-0.17.0.1/setup.py': b'# build seam fixture\n'}
    inspections = []
    def inspect(command, *, text):
        inspections.append(command)
        if command[1] == '-L':
            return 'proof.so:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n'
        minimum_field = 'minos' if minimum_command == 'LC_BUILD_VERSION' else 'version'
        os_field = '' if minimum == 'missing' else f' {minimum_field} {minimum if minimum not in {"ambiguous", "wrong-platform"} else "11.0"}\n'
        tools = (' platform ' + ('2' if minimum == 'wrong-platform' else '1')
                 + '\n ntools 1\n tool LD\n version 1053.12\n') if minimum_command == 'LC_BUILD_VERSION' else ''
        listed_command = 'LC_VERSION_MIN_IPHONEOS' if minimum == 'wrong-platform' and minimum_command == 'LC_VERSION_MIN_MACOSX' else minimum_command
        listing = (f'Load command 0\n cmd {listed_command}\n' + os_field + ' sdk 14.5\n' + tools
                   + 'Load command 1\n cmd LC_SOURCE_VERSION\n version 555.66\n')
        if minimum == 'ambiguous':
            listing += 'Load command 2\n cmd LC_VERSION_MIN_MACOSX\n version 11.0\n'
        return listing
    monkeypatch.setattr(prepare.subprocess, 'check_output', inspect)
    if binary_kind != 'x64':
        with pytest.raises(ValueError, match='architecture'):
            prepare.build_intel_mac(members, locked)
        assert inspections == []
    elif minimum != '11.0':
        with pytest.raises(ValueError, match='minimum|newer OS'):
            prepare.build_intel_mac(members, locked)
    else:
        assert prepare.build_intel_mac(members, locked) == bytes(raw)
        assert len(inspections) == 2
    assert '--plat-name=macosx_11_0_x86_64' in seen['command']
    assert '--py-limited-api=cp310' in seen['command']
    assert seen['env']['ARCHFLAGS'] == '-arch x86_64'
    assert seen['env']['MACOSX_DEPLOYMENT_TARGET'] == '11.0'
