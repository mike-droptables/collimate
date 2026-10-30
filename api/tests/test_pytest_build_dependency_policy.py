"""Exercise the selected npm-ci and gzip budget contracts through real executables."""
from __future__ import annotations

import json
import os
from pathlib import Path
import random
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def npm_project(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    for name in ('package.json', 'package-lock.json'):
        (project / name).write_text('{}')
    bins = tmp_path / 'bin'
    bins.mkdir()
    npm = bins / 'npm'
    npm.write_text('#!/bin/bash\nset -e\necho "$*" >> "$NPM_LOG"\n[ "${FAIL_CI:-0}" = 0 ] || exit 19\nmkdir -p node_modules\n')
    npm.chmod(0o755)
    env = os.environ | {'PATH': f'{bins}:/usr/bin:/bin', 'NPM_LOG': str(tmp_path / 'npm.log')}
    return project, env


def install(project, env):
    return subprocess.run(['bash', str(ROOT / 'build/npm_ci_if_stale.sh'), str(project)], env=env, capture_output=True, text=True, timeout=10)


def test_missing_current_and_both_manifest_changes__F1087_F1122_F1059_F1084(npm_project):
    project, env = npm_project
    lock = (project / 'package-lock.json').read_bytes()
    assert install(project, env).returncode == 0
    assert install(project, env).returncode == 0
    stamp = project / 'node_modules/.collimate-npm-ci-stamp'
    assert stamp.is_file()
    for name in ('package.json', 'package-lock.json'):
        future = stamp.stat().st_mtime_ns + 10_000_000
        os.utime(project / name, ns=(future, future))
        result = install(project, env)
        assert result.returncode == 0, result.stderr
        # Restore deliberately future-dated fixture bytes to a past timestamp.
        os.utime(project / name, ns=(1, 1))
    assert Path(env['NPM_LOG']).read_text().splitlines() == ['ci', 'ci', 'ci']
    assert (project / 'package-lock.json').read_bytes() == lock
    shutil.rmtree(project / 'node_modules')
    assert install(project, env).returncode == 0
    assert Path(env['NPM_LOG']).read_text().splitlines() == ['ci'] * 4


def test_node_change_reinstalls(npm_project, tmp_path):
    project, env = npm_project
    node = tmp_path / 'bin/node'
    node.write_text('#!/bin/sh\necho "$NODE_VERSION"\n')
    node.chmod(0o755)
    for version in ('v20.19.4', 'v20.19.4', 'v24.15.0'):
        assert install(project, env | {'NODE_VERSION': version}).returncode == 0
    assert Path(env['NPM_LOG']).read_text().splitlines() == ['ci', 'ci']


def test_failed_ci_does_not_mark_install_current__F1087_F1084(npm_project):
    project, env = npm_project
    result = install(project, env | {'FAIL_CI': '1'})
    assert result.returncode == 19
    assert not (project / 'node_modules/.collimate-npm-ci-stamp').exists()
    assert install(project, env).returncode == 0
    assert install(project, env).returncode == 0
    assert Path(env['NPM_LOG']).read_text().splitlines() == ['ci', 'ci']


def test_failed_refresh_invalidates_previous_success_stamp__F1087_F1084(npm_project):
    project, env = npm_project
    assert install(project, env).returncode == 0
    stamp = project / 'node_modules/.collimate-npm-ci-stamp'
    future = stamp.stat().st_mtime_ns + 10_000_000
    os.utime(project / 'package-lock.json', ns=(future, future))
    assert install(project, env | {'FAIL_CI': '1'}).returncode == 19
    assert not stamp.exists()
    os.utime(project / 'package-lock.json', ns=(1, 1))
    assert install(project, env).returncode == 0
    assert Path(env['NPM_LOG']).read_text().splitlines() == ['ci', 'ci', 'ci']


def budget(tmp_path, size, baseline=None, update=False):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    shutil.copy2(ROOT / 'frontend/scripts/check-bundle-size.mjs', scripts)
    assets = tmp_path / 'dist/assets'
    assets.mkdir(parents=True)
    # Deterministic incompressible bytes exercise real zlib gzip accounting.
    (assets / 'fixture.js').write_bytes(random.Random(1912).randbytes(size))
    if baseline is not None:
        (tmp_path / '.bundle-baseline.json').write_text(json.dumps({'totalGzipKb': baseline}))
    node = shutil.which('node')
    assert node, 'Node is required by the bundle checker'
    return subprocess.run([node, str(scripts / 'check-bundle-size.mjs'), *(['--update'] if update else [])], capture_output=True, text=True, timeout=10)


def test_actual_gzip_hard_ceiling__F1092(tmp_path):
    result = budget(tmp_path, 850 * 1024)
    assert result.returncode == 1
    assert 'hard ceiling 800 kB' in result.stderr


def test_growth_ceiling_stays_ten_percent__F1092(tmp_path):
    result = budget(tmp_path, 120 * 1024, baseline=100)
    assert result.returncode == 1
    assert 'ceiling 10%' in result.stderr


def test_small_gzip_bundle_passes__F1092(tmp_path):
    result = budget(tmp_path, 80 * 1024, baseline=100)
    assert result.returncode == 0, result.stderr


def test_baseline_update_cannot_bypass_hard_ceiling__F1092(tmp_path):
    result = budget(tmp_path, 850 * 1024, baseline=100, update=True)
    assert result.returncode == 1
    assert json.loads((tmp_path / '.bundle-baseline.json').read_text()) == {'totalGzipKb': 100}


def test_growth_refusal_requires_feature_evidence__P6R02(tmp_path):
    result = budget(tmp_path, 120 * 1024, baseline=100)
    assert result.returncode == 1
    assert json.loads((tmp_path / '.bundle-baseline.json').read_text()) == {'totalGzipKb': 100}
    assert 'landed feature' in result.stderr
    assert 'measured cost' in result.stderr
    assert 'ledger row' in result.stderr
    assert 'bundle:update' not in result.stderr
