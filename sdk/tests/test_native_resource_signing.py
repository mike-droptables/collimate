"""A macOS server environment's signed native resources (collimate._host.native_resources).

codesign rewrites the esbuild binaries and the QuickJS extension a mac
desktop package signs, so their bytes no longer hash to their pins (v0.1.1's
mac legs: "Native compiler resource hash mismatch" at the packaged server's
startup). The build checks each against its pin before signing and records,
beside each file it changed, the pin and the sha256 it was signed to; the SDK
admits exactly those bytes on macOS and nothing else anywhere. Simulated here
on the checkout's prepared resources: "signing" appends bytes, as codesign
changes them.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import struct
import sys

import pytest

from collimate import _renderer_compilers as compilers
from collimate._host import native_resources, verifier_engine as engine

ROOT = Path(__file__).resolve().parents[1]
PREPARED = ROOT / 'build/renderer_compilers'
VERSION = json.loads((ROOT / 'src/collimate/renderer_bundle_contract.json').read_text())['esbuild_version']


def sign(path: Path) -> tuple[str, str]:
    """``path`` rewritten as codesign would; (its pin, its signed sha256)."""
    pinned = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(path.read_bytes() + b'\0signature')
    return pinned, hashlib.sha256(path.read_bytes()).hexdigest()


def record(folder: Path, name: str, pinned: str, signed: str, **extra) -> Path:
    path = folder / native_resources.RECORD
    path.write_text(json.dumps({'schema': 1, 'files': {name: {'pinned': pinned, 'signed': signed}}, **extra}))
    return path


@pytest.fixture
def mac_compilers(tmp_path, monkeypatch):
    """The prepared compilers as an installed package's, on an Apple silicon Mac."""
    shutil.copytree(PREPARED, tmp_path / 'renderer_compilers')
    monkeypatch.setattr(compilers, '__file__', str(tmp_path / '_renderer_compilers.py'))
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(compilers.platform, 'machine', lambda: 'arm64')
    return tmp_path / 'renderer_compilers'


def test_a_signed_compiler_is_admitted_on_macos_only_by_the_record_of_its_own_pin(mac_compilers, monkeypatch):
    esbuild = mac_compilers / 'darwin-arm64/esbuild'
    assert compilers.compiler_path(VERSION) == esbuild.resolve()
    pinned, signed = sign(esbuild)
    with pytest.raises(ValueError, match='Native compiler resource hash mismatch'):
        compilers.compiler_path(VERSION)
    record(esbuild.parent, 'esbuild', pinned, signed)
    assert compilers.compiler_path(VERSION) == esbuild.resolve()
    # Changed after it was signed: the record names other bytes.
    esbuild.write_bytes(esbuild.read_bytes() + b'tampered')
    with pytest.raises(ValueError, match='hash mismatch'):
        compilers.compiler_path(VERSION)


@pytest.mark.parametrize('case', ['other pin', 'other file', 'other schema', 'extra key', 'entry key', 'unsigned',
                                  'not json', 'symlink', 'not mach-o', 'linux'])
def test_no_other_record_admits_a_changed_compiler(mac_compilers, monkeypatch, case):
    esbuild = mac_compilers / 'darwin-arm64/esbuild'
    if case == 'not mach-o':
        esbuild.write_bytes(b'#!/bin/sh\n' + esbuild.read_bytes()[4:])
        manifest = json.loads((mac_compilers / 'manifest.json').read_text())
        manifest['targets']['darwin-arm64']['sha256'] = hashlib.sha256(esbuild.read_bytes()).hexdigest()
        (mac_compilers / 'manifest.json').write_text(json.dumps(manifest))
    pinned, signed = sign(esbuild)
    folder = esbuild.parent
    if case == 'other pin':
        record(folder, 'esbuild', 'ab' * 32, signed)
    elif case == 'other file':
        record(folder, 'README.md', pinned, signed)
    elif case == 'other schema':
        (folder / 'codesign.json').write_text(json.dumps({'schema': 2, 'files': {'esbuild': {'pinned': pinned, 'signed': signed}}}))
    elif case == 'extra key':
        record(folder, 'esbuild', pinned, signed, identity='-')
    elif case == 'entry key':
        (folder / 'codesign.json').write_text(json.dumps(
            {'schema': 1, 'files': {'esbuild': {'pinned': pinned, 'signed': signed, 'by': '-'}}}))
    elif case == 'unsigned':
        record(folder, 'esbuild', pinned, pinned)
    elif case == 'not json':
        (folder / 'codesign.json').write_text('{')
    elif case == 'symlink':
        record(mac_compilers, 'esbuild', pinned, signed)
        (folder / 'codesign.json').symlink_to(mac_compilers / 'codesign.json')
    else:
        record(folder, 'esbuild', pinned, signed)
    if case == 'linux':
        # The same record and the same bytes off macOS: exactly the pin, as before.
        monkeypatch.setattr(sys, 'platform', 'linux')
        shutil.copy(esbuild, mac_compilers / 'linux-arm64/esbuild')
        record(mac_compilers / 'linux-arm64', 'esbuild', json.loads(
            (mac_compilers / 'manifest.json').read_text())['targets']['linux-arm64']['sha256'], signed)
    with pytest.raises(ValueError, match='hash mismatch'):
        compilers.compiler_path(VERSION)


def darwin_x64_payload(folder: Path) -> Path:
    """A darwin-x64 verifier payload (the contract's source build, pinned by its
    manifest alone) whose extension is a Mach-O header for that target."""
    locked = engine.contract()
    folder.mkdir()
    extension = b'\xcf\xfa\xed\xfe' + struct.pack('<I', 0x01000007) + bytes(64)
    (folder / '_quickjs.abi3.so').write_bytes(extension)
    linux = engine.payload_directory('linux-x64')
    for name in locked['license_sha256']:
        shutil.copy(linux / name, folder / name)
    (folder / 'manifest.json').write_text(json.dumps({
        'schema': 1, 'binding_version': locked['binding_version'], 'abi': locked['abi'], 'target': 'darwin-x64',
        'platform_tag': locked['targets']['darwin-x64']['platform_tag'], 'source_sha256': locked['source']['sha256'],
        'files': {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                  for name in ('_quickjs.abi3.so', *locked['license_sha256'])}}))
    return folder


def test_a_signed_verifier_extension_is_admitted_on_macos_by_its_record(tmp_path, monkeypatch):
    payload = darwin_x64_payload(tmp_path / 'darwin-x64')
    monkeypatch.setattr(sys, 'platform', 'darwin')
    assert engine.verify_payload(payload, 'darwin-x64')['target'] == 'darwin-x64'
    pinned, signed = sign(payload / '_quickjs.abi3.so')
    with pytest.raises(ValueError, match='integrity'):
        engine.verify_payload(payload, 'darwin-x64')
    record(payload, '_quickjs.abi3.so', pinned, signed)
    assert engine.verify_payload(payload, 'darwin-x64')['files']['_quickjs.abi3.so'] == pinned
    # Its architecture is still checked on the signed bytes.
    monkeypatch.setattr(sys, 'platform', 'linux')
    with pytest.raises(ValueError, match='integrity'):
        engine.verify_payload(payload, 'darwin-x64')
    # A license is never Mach-O, so no record admits a changed one.
    monkeypatch.setattr(sys, 'platform', 'darwin')
    license_pin, license_changed = sign(payload / 'LICENSE.quickjs-ng')
    (payload / 'codesign.json').write_text(json.dumps({'schema': 1, 'files': {
        '_quickjs.abi3.so': {'pinned': pinned, 'signed': signed},
        'LICENSE.quickjs-ng': {'pinned': license_pin, 'signed': license_changed}}}))
    with pytest.raises(ValueError, match='integrity'):
        engine.verify_payload(payload, 'darwin-x64')


def test_the_build_lists_every_pinned_resource_strictly_before_signing(tmp_path):
    shutil.copytree(PREPARED, tmp_path / 'renderer_compilers')
    payloads = tmp_path / 'native_verifier'
    shutil.copytree(engine.payload_root(), payloads)
    darwin_x64_payload(payloads / 'darwin-x64')
    found = native_resources.pinned(tmp_path / 'renderer_compilers', payloads)
    manifest = json.loads((PREPARED / 'manifest.json').read_text())
    assert {path.relative_to(tmp_path).as_posix() for path in found} == {
        *(f'renderer_compilers/{entry["file"]}' for entry in manifest['targets'].values()),
        *(f'native_verifier/{folder.name}/{name}' for folder in payloads.iterdir()
          for name in ('_quickjs.abi3.so', 'LICENSE.quickjs-ng', 'LICENSE.quickjs-engine'))}
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == pin for path, pin in found.items())
    esbuild = tmp_path / 'renderer_compilers/darwin-x64/esbuild'
    pinned, signed = sign(esbuild)
    with pytest.raises(ValueError, match='differs from its pin'):
        native_resources.pinned(tmp_path / 'renderer_compilers', payloads)
    # The record that admits it at runtime is refused before signing.
    record(esbuild.parent, 'esbuild', pinned, signed)
    esbuild.write_bytes(esbuild.read_bytes().removesuffix(b'\0signature'))
    with pytest.raises(ValueError, match='exists before anything was signed'):
        native_resources.pinned(tmp_path / 'renderer_compilers', payloads)


def test_the_checkouts_resources_pass_the_runtime_check(capsys):
    assert native_resources.main(['check']) == 0
    assert 'by their signing record' in capsys.readouterr().out
    assert native_resources.main(['unknown']) == 2
