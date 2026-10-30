#!/usr/bin/env python3
"""Explicitly prepare hash-pinned native verifier payloads, never at install/runtime."""
from __future__ import annotations

import argparse
import hashlib
import io
import importlib.metadata
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from collimate._host.verifier_engine import contract, native_target, verify_payload, verify_binary_target  # noqa: E402
OUTPUT = ROOT / 'src/collimate/native_verifier'
MAX_ARCHIVE = 32 * 1024 * 1024


def fetch(entry, cache, offline):
    path = cache / entry['filename']
    if path.exists():
        raw = path.read_bytes()
    elif offline:
        raise ValueError(f"Missing offline native archive: {entry['filename']}")
    else:
        with urllib.request.urlopen(entry['url'], timeout=30) as response:
            raw = response.read(MAX_ARCHIVE + 1)
    if len(raw) > MAX_ARCHIVE or hashlib.sha256(raw).hexdigest() != entry['sha256']:
        raise ValueError('Native archive hash or size mismatch')
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def wheel_extension(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        matches = [entry for entry in archive.infolist() if entry.filename == '_quickjs.abi3.so']
        if len(matches) != 1 or matches[0].file_size > MAX_ARCHIVE:
            raise ValueError('Native wheel must contain one bounded stable-ABI extension')
        entry = matches[0]
        mode = entry.external_attr >> 16
        if mode & 0o170000 not in (0, 0o100000):
            raise ValueError('Native wheel extension must be regular')
        return archive.read(entry)


def source_members(raw, prefix="quickjs_ng-0.17.0.1"):
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        result = {}
        total = 0
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] != prefix or '..' in parts or Path(member.name).is_absolute():
                raise ValueError('Unsafe native source member path')
            if member.isdir():
                continue
            if not member.isfile() or member.name in result or member.size > MAX_ARCHIVE:
                raise ValueError('Native source members must be unique bounded regular files')
            total += member.size
            if total > MAX_ARCHIVE:
                raise ValueError('Native source expansion exceeds its byte limit')
            stream = archive.extractfile(member)
            assert stream is not None
            result[member.name] = stream.read()
        return result


def complete_intel_source(members, locked, cache, offline):
    """Supply only omitted headers from the exact matching official engine revision."""
    pinned = locked['source_build']['upstream']
    upstream_prefix = 'quickjs-' + pinned['revision'] + '/'
    upstream = source_members(fetch(pinned, cache, offline), prefix=upstream_prefix[:-1])
    engine_prefix = 'quickjs_ng-0.17.0.1/upstream-quickjs/'
    for name, raw in members.items():
        if name.startswith(engine_prefix):
            if upstream.get(upstream_prefix + name.removeprefix(engine_prefix)) != raw:
                raise ValueError('Binding source differs from its pinned upstream engine revision')
    completed = dict(members)
    for name, digest in pinned['headers'].items():
        raw = upstream.get(upstream_prefix + name)
        if raw is None or hashlib.sha256(raw).hexdigest() != digest or engine_prefix + name in members:
            raise ValueError('Omitted engine header differs from its pinned upstream source')
        completed[engine_prefix + name] = raw
    atomics = completed[engine_prefix + 'quickjs-c-atomics.h']
    if atomics[:atomics.index(b'*/') + 2].decode() != pinned['license_notice']:
        raise ValueError('Engine license notice differs from pinned upstream header')
    # System headers are supplied by the native toolchain; every quoted
    # project include must be present before starting that compiler.
    for name, raw in completed.items():
        if name.endswith(('.c', '.h')):
            for include in re.findall(rb'^\s*#\s*include\s*"([^"\n]+)"', raw, re.MULTILINE):
                wanted = str(Path(name).parent / include.decode('ascii'))
                if wanted not in completed:
                    raise ValueError(f'Prepared engine source is missing project include: {wanted}')
    return completed


def build_intel_mac(members, locked):
    if native_target() != 'darwin-x64':
        raise ValueError('The pinned Intel Mac source build requires its native runner')
    for name in ('setuptools', 'wheel'):
        if importlib.metadata.version(name) != locked['source_build'][name]:
            raise ValueError(f'Explicit source build requires pinned {name}')
    with tempfile.TemporaryDirectory(prefix='quickjs-build-') as scratch:
        root = Path(scratch)
        for name, raw in members.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        source = root / 'quickjs_ng-0.17.0.1'
        env = {**os.environ, 'MACOSX_DEPLOYMENT_TARGET': locked['source_build']['deployment_target'],
               'ARCHFLAGS': '-arch x86_64'}
        # python.org runners advertise universal2 even when ARCHFLAGS builds
        # only Intel. Select that actual thin target; verify its bytes below.
        platform_tag = locked['targets']['darwin-x64']['platform_tag']
        subprocess.run([sys.executable, 'setup.py', 'bdist_wheel', '--py-limited-api=cp310',
                        '--plat-name=' + platform_tag],
                       cwd=source, env=env, check=True)
        wheels = list((source / 'dist').glob('*-cp310-abi3-macosx_11_0_x86_64.whl'))
        if len(wheels) != 1:
            raise ValueError('Source build did not produce the requested Intel Mac stable-ABI wheel')
        binary = wheel_extension(wheels[0].read_bytes())
        verify_binary_target(binary, 'darwin-x64')
        shared = source / 'native-proof.so'
        shared.write_bytes(binary)
        linkage = subprocess.check_output(['otool', '-L', str(shared)], text=True)
        print(linkage, flush=True)
        dependencies = [line.strip().split(' (', 1)[0] for line in linkage.splitlines()[1:]]
        if not dependencies or any(not item.startswith('/usr/lib/') for item in dependencies):
            raise ValueError('Intel Mac engine links a non-system dynamic library')
        commands = subprocess.check_output(['otool', '-l', str(shared)], text=True)
        print(commands, flush=True)
        minimums = []
        for block in re.split(r'(?m)^[ \t]*cmd[ \t]+', commands)[1:]:
            kind = block.splitlines()[0].strip()
            if kind not in {'LC_BUILD_VERSION', 'LC_VERSION_MIN_MACOSX'}:
                continue
            field = 'minos' if kind == 'LC_BUILD_VERSION' else 'version'
            values = re.findall(r'(?m)^[ \t]*' + field + r'[ \t]+(\d+\.\d+(?:\.\d+)?)[ \t]*$', block)
            if len(values) != 1:
                raise ValueError('Intel Mac engine has missing or ambiguous macOS minimum')
            if kind == 'LC_BUILD_VERSION' and not re.search(r'(?m)^[ \t]*platform[ \t]+(?:1|MACOS)[ \t]*$', block):
                raise ValueError('Intel Mac engine has no macOS minimum platform')
            minimums.extend(values)
        if len(minimums) != 1:
            raise ValueError('Intel Mac engine has missing or ambiguous macOS minimum')
        if any(tuple(map(int, minimum.split('.')[:2])) > (11, 0) for minimum in minimums):
            raise ValueError('Intel Mac engine requires a newer OS than its wheel tag')
        return binary


def provision_sdk_wheel(path, target, out):
    """Provision an existing first-party wheel, without installing or compiling it."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        required = ('manifest.json', '_quickjs.abi3.so', 'LICENSE.quickjs-ng', 'LICENSE.quickjs-engine')
        files = {}
        for name in required:
            key = 'collimate/native_verifier/' + target + '/' + name
            matches = [entry for entry in archive.infolist() if entry.filename == key]
            if len(matches) != 1 or matches[0].file_size > MAX_ARCHIVE:
                raise ValueError('SDK wheel has an ambiguous or oversized native resource')
            mode = matches[0].external_attr >> 16
            if mode & 0o170000 not in (0, 0o100000):
                raise ValueError('SDK native resources must be regular files')
            files[name] = archive.read(matches[0])
        if len(names) != len(set(names)):
            raise ValueError('SDK wheel contains duplicate paths')
    # Check in an isolated directory before replacing the usable prepared payload.
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='native-provision-', dir=out) as scratch:
        prepared = Path(scratch)
        for name, raw in files.items():
            (prepared / name).write_bytes(raw)
        record = verify_payload(prepared, target)
        destination = out / target
        destination.mkdir(parents=True, exist_ok=True)
        for name, raw in files.items():
            (destination / name).write_bytes(raw)
    return record


def prepare(target, out, cache, offline=False, build_source=False):
    locked = contract()
    entry = locked['targets'][target]
    if entry.get('source_build') and not build_source:
        return verify_payload(out / target, target)
    source = source_members(fetch(locked['source'], cache, offline))
    prefix = 'quickjs_ng-0.17.0.1/'
    licenses = {
        'LICENSE.quickjs-ng': source[prefix + 'LICENSE'],
        'LICENSE.quickjs-engine': b'\n\n'.join(raw[:raw.index(b'*/') + 2]
            for name, raw in sorted(source.items())
            if name.startswith(prefix + 'upstream-quickjs/') and raw.startswith(b'/*') and b'Permission is hereby granted' in raw[:4096])
            + b'\n\n' + locked['source_build']['upstream']['license_notice'].encode(),
    }
    if not licenses['LICENSE.quickjs-engine']:
        raise ValueError('Pinned engine license notices are missing')
    binary = (build_intel_mac(complete_intel_source(source, locked, cache, offline), locked) if entry.get('source_build')
              else wheel_extension(fetch(entry, cache, offline)))
    files = {'_quickjs.abi3.so': binary, **licenses}
    record = {'schema': 1, 'binding_version': locked['binding_version'], 'abi': locked['abi'],
              'target': target, 'platform_tag': entry['platform_tag'],
              'source_sha256': locked['source']['sha256'],
              'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
    directory = out / target
    directory.mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        (directory / name).write_bytes(raw)
    (directory / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    return verify_payload(directory, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=OUTPUT)
    parser.add_argument('--cache', type=Path, default=ROOT / 'build/verifier-archives')
    parser.add_argument('--target', choices=tuple(contract()['targets']))
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--build-source', action='store_true')
    parser.add_argument('--from-sdk-wheel', type=Path, help='Provision the host private payload from an existing SDK wheel')
    args = parser.parse_args()
    targets = (tuple(contract()['targets']) if args.all else (args.target,) if args.target
               else tuple(dict.fromkeys(('linux-x64', 'linux-arm64', native_target()))))
    if args.from_sdk_wheel:
        if args.all or args.build_source or args.verify_only:
            parser.error('--from-sdk-wheel selects one target and cannot build or verify-only')
        print(json.dumps(provision_sdk_wheel(args.from_sdk_wheel, args.target or native_target(), args.out), sort_keys=True))
        return
    for target in targets:
        result = (verify_payload(args.out / target, target) if args.verify_only
                  else prepare(target, args.out, args.cache, args.offline, args.build_source))
        print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
