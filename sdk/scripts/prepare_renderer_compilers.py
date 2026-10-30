#!/usr/bin/env python3
"""Prepare the four exact locked native compiler resources; never run at runtime."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import stat
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ('linux-x64', 'linux-arm64', 'darwin-x64', 'darwin-arm64')
OUTPUT = ROOT / 'build' / 'renderer_compilers'


def locked_packages():
    version = json.loads((ROOT / 'src/collimate/renderer_bundle_contract.json').read_text())['esbuild_version']
    packages = json.loads((ROOT / 'scripts/package-lock.json').read_text())['packages']
    result = {}
    for target in TARGETS:
        package = packages['node_modules/@esbuild/' + target]
        if package['version'] != version or not package['resolved'].startswith('https://registry.npmjs.org/@esbuild/'):
            raise ValueError('Native compiler lock differs from canonical pin')
        result[target] = package
    return version, result


def extract_artifact(raw: bytes, integrity: str) -> tuple[bytes, bytes]:
    algorithm, encoded = integrity.split('-', 1)
    if algorithm != 'sha512' or hashlib.sha512(raw).digest() != base64.b64decode(encoded, validate=True):
        raise ValueError('Native compiler archive integrity mismatch')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        members = archive.getmembers()
        result = []
        for name in ('package/bin/esbuild', 'package/README.md'):
            selected = [m for m in members if m.name == name]
            if len(selected) != 1 or not selected[0].isfile() or selected[0].size > 32 * 1024 * 1024:
                raise ValueError('Native compiler archive member is missing, duplicated or not regular')
            stream = archive.extractfile(selected[0])
            assert stream is not None
            result.append(stream.read())
        return result[0], result[1]


def verify(directory: Path = OUTPUT) -> dict:
    version, locked = locked_packages()
    manifest_path = directory / 'manifest.json'
    if not stat.S_ISREG(manifest_path.lstat().st_mode):
        raise ValueError('Native compiler manifest must be regular')
    manifest = json.loads(manifest_path.read_text())
    if set(manifest) != {'schema', 'version', 'targets'} or manifest['schema'] != 1 or manifest['version'] != version or set(manifest['targets']) != set(TARGETS):
        raise ValueError('Native compiler resource manifest is incomplete or mismatched')
    for target, entry in manifest['targets'].items():
        if set(entry) != {'file', 'sha256', 'integrity'} or entry['file'] != target + '/esbuild' or entry['integrity'] != locked[target]['integrity']:
            raise ValueError('Native compiler resource entry is invalid')
        path = directory / entry['file']
        if not stat.S_ISREG(path.lstat().st_mode) or not path.stat().st_mode & stat.S_IXUSR:
            raise ValueError('Native compiler resource must be an executable regular file')
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Native compiler resource hash mismatch')
    if not (directory / 'LICENSE.esbuild').is_file():
        raise ValueError('Native compiler license missing')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=OUTPUT)
    parser.add_argument('--cache', type=Path, default=ROOT / 'build/compiler-archives')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if args.verify_only:
        verify(args.out)
        return
    version, packages = locked_packages()
    args.cache.mkdir(parents=True, exist_ok=True)
    manifest = {'schema': 1, 'version': version, 'targets': {}}
    staged = {}
    for target, package in packages.items():
        cached = args.cache / f'{target}-{version}.tgz'
        if cached.exists():
            raw = cached.read_bytes()
        elif args.offline:
            raise ValueError(f'Offline compiler archive is absent: {target}')
        else:
            with urllib.request.urlopen(package['resolved'], timeout=30) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise ValueError('Compiler archive is too large')
        binary, readme = extract_artifact(raw, package['integrity'])
        cached.write_bytes(raw)
        staged[target] = (binary, readme)
        manifest['targets'][target] = {'file': target + '/esbuild', 'sha256': hashlib.sha256(binary).hexdigest(), 'integrity': package['integrity']}
    # All archives verified before publishing any compiler resource.
    for target, (binary, readme) in staged.items():
        folder = args.out / target
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'esbuild').write_bytes(binary)
        (folder / 'esbuild').chmod(0o755)
        (folder / 'README.md').write_bytes(readme)
    # Upstream MIT text is also included in the pinned main esbuild package.
    license_text = (ROOT / 'scripts/esbuild-LICENSE.md').read_bytes()
    (args.out / 'LICENSE.esbuild').write_bytes(license_text)
    (args.out / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    verify(args.out)
    print(f'Prepared {len(TARGETS)} esbuild {version} targets ({sum(len(v[0]) for v in staged.values()):,} executable bytes)')


if __name__ == '__main__':
    main()
