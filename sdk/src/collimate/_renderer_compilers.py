"""Select verified installed native resources, never a host executable override."""
from __future__ import annotations

import hashlib
import json
import platform
import stat
import sys
from pathlib import Path


def compiler_path(version: str) -> Path:
    system = {'linux': 'linux', 'darwin': 'darwin'}.get(sys.platform)
    machine = {'x86_64': 'x64', 'AMD64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
    if system is None or machine is None:
        raise ValueError(f'Native renderer compiler is unavailable for {sys.platform}/{platform.machine()}')
    root = Path(__file__).with_name('renderer_compilers')
    if not root.is_dir():
        root = Path(__file__).resolve().parents[2] / 'build/renderer_compilers'
    manifest_file = root / 'manifest.json'
    if root.is_symlink() or not stat.S_ISREG(manifest_file.lstat().st_mode):
        raise ValueError('Native compiler resources are missing or invalid')
    manifest = json.loads(manifest_file.read_text())
    if manifest.get('schema') != 1 or manifest.get('version') != version:
        raise ValueError('Native compiler manifest version mismatch')
    target = system + '-' + machine
    entry = manifest['targets'][target]
    if entry['file'] != target + '/esbuild':
        raise ValueError('Native compiler resource path mismatch')
    executable = root / target / 'esbuild'
    if executable.parent.is_symlink() or not stat.S_ISREG(executable.lstat().st_mode) or not executable.stat().st_mode & stat.S_IXUSR:
        raise ValueError('Native compiler must be an executable regular resource')
    if hashlib.sha256(executable.read_bytes()).hexdigest() != entry['sha256']:
        raise ValueError('Native compiler resource hash mismatch')
    return executable.resolve()
