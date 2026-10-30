"""Clean-checkout prerequisites must exist before the SDK build begins."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize('omitted', [None, 'compiler', 'verifier'])
def test_fresh_sdk_checkout_prepares_verified_native_resources_before_install__P4F28_P4F05(tmp_path, omitted):
    app_source = Path(__file__).resolve().parents[2]
    sdk_source = app_source / 'sdk'
    app = tmp_path / 'collimatedev'
    sdk = app / 'sdk'
    (app / 'build').mkdir(parents=True)
    (sdk / 'scripts').mkdir(parents=True)
    (sdk / 'src/collimate/_host').mkdir(parents=True)
    shutil.copy2(app_source / 'build/prepare_sdk_checkout.sh', app / 'build/prepare_sdk_checkout.sh')
    for name in ('prepare_renderer_compilers.py', 'prepare_verifier_engine.py', 'package-lock.json', 'esbuild-LICENSE.md'):
        shutil.copy2(sdk_source / 'scripts' / name, sdk / 'scripts' / name)
    for name in ('__init__.py', '_host/__init__.py', '_host/verifier_engine.py',
                 'verifier_engine_contract.json', 'renderer_bundle_contract.json'):
        shutil.copy2(sdk_source / 'src/collimate' / name, sdk / 'src/collimate' / name)
    # These are the actual integrity-pinned archives downloaded by normal setup.
    # A private copy keeps the preparation script's cache writes test-local.
    shutil.copytree(sdk_source / 'build/compiler-archives', sdk / 'build/compiler-archives')
    shutil.copytree(sdk_source / 'build/verifier-archives', sdk / 'build/verifier-archives')
    intel_payload = sdk_source / 'src/collimate/native_verifier/darwin-x64'
    if intel_payload.is_dir():
        # Routine Intel source setup requires its explicitly provisioned CI
        # payload; normal preparation verifies it and never invokes a compiler.
        shutil.copytree(intel_payload, sdk / 'src/collimate/native_verifier/darwin-x64')
    if omitted is not None:
        script = app / 'build/prepare_sdk_checkout.sh'
        name = 'prepare_renderer_compilers.py' if omitted == 'compiler' else 'prepare_verifier_engine.py'
        command = 'python3 scripts/' + name + '\n'
        content = script.read_text()
        assert content.count(command) == 1
        script.write_text(content.replace(command, ''))
    tools = tmp_path / 'tools'
    tools.mkdir()
    for name in ('npm',):
        tool = tools / name
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o755)
    python = tools / 'python3'
    python.write_text(f'#!{sys.executable}\n' + '''import pathlib, subprocess, sys
arguments = sys.argv[1:]
# The built outputs and the snapshot bodies are git-ignored inputs the real
# script builds and fetches; this test covers the native-resource gate only.
if pathlib.Path(arguments[0]).name in {'build_outputs.py', 'snapshot_store.py'}:
    raise SystemExit(0)
if pathlib.Path(arguments[0]).name in {'prepare_renderer_compilers.py', 'prepare_verifier_engine.py'}:
    arguments.append('--offline')
raise SystemExit(subprocess.run([sys.executable, *arguments]).returncode)
''')
    python.chmod(0o755)
    uv = tools / 'uv'
    uv.write_text(f'#!{sys.executable}\n' + '''import pathlib, subprocess, sys
assert sys.argv[1:] == ['sync', '--all-packages', '--all-groups'], sys.argv
sdk = pathlib.Path('sdk').resolve()
sys.path.insert(0, str(sdk / 'src'))
from collimate._host.verifier_engine import CONTRACT
assert CONTRACT.resolve().parent == sdk / 'src/collimate'
# Exercise the same real verification that Hatch runs at editable install.
for name in ('prepare_renderer_compilers.py', 'prepare_verifier_engine.py'):
    subprocess.run([sys.executable, str(sdk / 'scripts' / name), '--verify-only'], check=True)
print('Verified native resources before SDK install', flush=True)
# Stop at the workspace sync, the script's last step.
raise SystemExit(78)
''')
    uv.chmod(0o755)
    result = subprocess.run(['bash', 'build/prepare_sdk_checkout.sh'], cwd=app,
                            env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']},
                            capture_output=True, text=True, timeout=60)
    if omitted is None:
        assert result.returncode == 78, result.stdout + result.stderr
        assert 'Verified native resources before SDK install' in result.stdout
    else:
        assert result.returncode not in (0, 78), result.stdout + result.stderr
        assert 'manifest.json' in result.stderr
        assert 'Verified native resources before SDK install' not in result.stdout
