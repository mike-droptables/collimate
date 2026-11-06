# SPDX-License-Identifier: MIT
"""Shared Node renderer test tools; each shim keeps its entrypoint and contract."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._workspace import WORKSPACE

REPO = Path(__file__).resolve().parents[1]
BUNDLER = REPO / 'scripts/bundle_renderer_test.mjs'


def tsx_loader(package: Path) -> Path | None:
    """Resolve the package's ESM import hook to an absolute path."""
    manifest = package / 'package.json'
    if not manifest.is_file():
        return None
    try:
        exports = json.loads(manifest.read_text()).get('exports', {})
        dot = exports.get('.') if isinstance(exports, dict) else None
    except (json.JSONDecodeError, OSError):
        dot = None
    if isinstance(dot, str):
        candidate = (package / dot).resolve()
        if candidate.is_file():
            return candidate
    candidate = (package / 'dist/loader.mjs').resolve()
    return candidate if candidate.is_file() else None


LOADER = tsx_loader(REPO / 'scripts/node_modules/tsx')


def node_renderer_skip(test_file, reason):
    """Skip only when the Node tools are absent: a test file that does not
    exist is a stale path, and fails collection instead of skipping."""
    assert Path(test_file).is_file(), f'node renderer test file not found: {test_file}'
    ready = (shutil.which('node') is not None
             and any((REPO / location).exists() for location in
                     ('scripts/node_modules/esbuild', 'packages/renderer-hooks/node_modules/esbuild'))
             and LOADER is not None and BUNDLER.is_file())
    return pytest.mark.skipif(not ready, reason=reason)


def run_node_renderer(entrypoint, bundle, test_file, env_key, *, label, bundle_label='main.tsx', env=None):
    """Bundle one actual source graph and run its node test from the workspace
    root, where the renderer tests' paths start; propagate both failures."""
    assert LOADER is not None  # narrowed by each shim's tool readiness marker
    build = subprocess.run(['node', str(BUNDLER), str(entrypoint), str(bundle)],
                           capture_output=True, text=True, cwd=str(WORKSPACE))
    assert build.returncode == 0 and bundle.is_file(), (
        f'bundling {bundle_label} failed\n--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}')
    process = subprocess.run(['node', '--import', LOADER.as_uri(), '--test', str(test_file)],
                             capture_output=True, text=True, cwd=str(WORKSPACE),
                             env={**os.environ, env_key: str(bundle), **(env or {})})
    assert process.returncode == 0, (
        f'{label} failed\n--- stdout ---\n{process.stdout}\n--- stderr ---\n{process.stderr}')
