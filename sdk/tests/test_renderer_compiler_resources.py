"""Supply/build validation, distinct from execution on untested platforms."""
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tarfile

import pytest

from collimate._host.renderer_bundle import CONTRACT

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('compiler_prep', ROOT / 'scripts/prepare_renderer_compilers.py')
prep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prep)


def test_all_four_locked_resources_are_complete_and_verified__F1984_F1868():
    manifest = prep.verify()
    assert set(manifest['targets']) == {'linux-x64', 'linux-arm64', 'darwin-x64', 'darwin-arm64'}
    assert manifest['version'] == CONTRACT['esbuild_version']


def test_resource_hash_and_missing_target_fail_closed__F1984(tmp_path):
    shutil.copytree(prep.OUTPUT, tmp_path / 'resources')
    root = tmp_path / 'resources'
    executable = root / 'linux-x64/esbuild'
    executable.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='hash mismatch'):
        prep.verify(root)
    manifest = json.loads((root / 'manifest.json').read_text())
    del manifest['targets']['darwin-arm64']
    (root / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='incomplete'):
        prep.verify(root)


def archive(kind):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as stream:
        for name in ('package/bin/esbuild', 'package/README.md'):
            member = tarfile.TarInfo(name)
            raw = b'safe'
            if name.endswith('esbuild') and kind == 'symlink':
                member.type = tarfile.SYMTYPE
                member.linkname = '/etc/passwd'
                stream.addfile(member)
            else:
                member.size = len(raw)
                stream.addfile(member, io.BytesIO(raw))
                if name.endswith('esbuild') and kind == 'duplicate':
                    stream.addfile(member, io.BytesIO(raw))
    raw = out.getvalue()
    return raw, 'sha512-' + base64.b64encode(hashlib.sha512(raw).digest()).decode()


@pytest.mark.parametrize('kind', ['symlink', 'duplicate'])
def test_artifact_links_and_duplicates_never_extracted__F1984_P0F21(kind):
    raw, integrity = archive(kind)
    with pytest.raises(ValueError, match='regular'):
        prep.extract_artifact(raw, integrity)


def test_artifact_integrity_checked_before_archive_parse__F1984_P0F21():
    raw, integrity = archive('regular')
    assert prep.extract_artifact(raw, integrity) == (b'safe', b'safe')
    with pytest.raises(ValueError, match='integrity'):
        prep.extract_artifact(raw+b'tampered', integrity)


def test_runtime_tampering_and_unsupported_platform_refuse_before_exec__F1984(monkeypatch, tmp_path):
    from collimate import _renderer_compilers as resources
    from collimate._host import renderer_bundle
    root = tmp_path / 'renderer_compilers'
    shutil.copytree(prep.OUTPUT, root)
    monkeypatch.setattr(resources, '__file__', str(tmp_path / '_renderer_compilers.py'))
    monkeypatch.setattr(resources.sys, 'platform', 'linux')
    monkeypatch.setattr(resources.platform, 'machine', lambda: 'x86_64')
    (root / 'linux-x64/esbuild').write_bytes(b'not the pinned executable')
    with pytest.raises(renderer_bundle.RendererCompileError, match='resources unavailable or invalid'):
        renderer_bundle.bundle_renderer({'main.ts': b'export default 1;'}, entrypoint='main.ts')
    monkeypatch.setattr(resources.platform, 'machine', lambda: 'unknown-target')
    with pytest.raises(renderer_bundle.RendererCompileError, match='resources unavailable or invalid'):
        renderer_bundle.bundle_renderer({'main.ts': b'export default 1;'}, entrypoint='main.ts')
