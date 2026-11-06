"""Actual locked and installed hooks compiler resolution for the patched toolchain."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from collimate._host.renderer_bundle import CONTRACT

HOOKS = Path(__file__).resolve().parents[1] / 'packages/renderer-hooks'


def test_hooks_locked_compiler_uses_patched_canonical_version__P3F06():
    lock = json.loads((HOOKS / 'package-lock.json').read_text())
    assert lock['packages']['node_modules/esbuild']['version'] == CONTRACT['esbuild_version']


@pytest.mark.skipif(not shutil.which('node') or not (HOOKS / 'node_modules/tsup').is_dir(),
                    reason='installed hooks tooling and Node required')
def test_tsup_resolves_actual_patched_compiler__P3F06():
    probe = """const {createRequire}=require('module');
const fromTsup=createRequire(require.resolve('tsup/package.json'));
process.stdout.write(fromTsup('esbuild').version);"""
    result = subprocess.run(['node', '-e', probe], cwd=HOOKS, capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout == CONTRACT['esbuild_version']
