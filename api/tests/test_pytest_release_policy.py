"""The release is one distribution, collimate, at one version, checked before and after it is built."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'build/release_policy.py'


def _tree(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="collimate"\nversion="1.2.3"\n')
    return tmp_path


def _run(root, command, *args, event='push', tag='v1.2.3', ref_type='tag'):
    """The script under release.yml's environment: a pushed tag arrives as the
    runner's GITHUB_REF_NAME (REF_TYPE tag; a main push's is REF_TYPE branch),
    a dispatch rehearsal's as its DISPATCH_TAG input."""
    env = {key: value for key, value in os.environ.items() if key not in ('GITHUB_REF_NAME', 'DISPATCH_TAG', 'REF_TYPE')}
    env.update({'EVENT_NAME': event, 'REF_TYPE': ref_type, 'GITHUB_REF_NAME' if event == 'push' else 'DISPATCH_TAG': tag})
    return subprocess.run([sys.executable, str(SCRIPT), command, '--root', str(root), *args],
                          env=env, capture_output=True, text=True)


@pytest.mark.parametrize('event', ['push', 'workflow_dispatch'])
def test_tag_guard_holds_the_tag_to_the_pyproject_version_on_either_trigger_F1056(tmp_path, event):
    """A pushed tag and a rehearsal's tag input are held to the one version,
    pyproject.toml's, alike; nothing else starts a release. The desktop
    package carries no version of its own to check."""
    root = _tree(tmp_path)
    assert _run(root, 'guard', event=event).returncode == 0
    for tag in ('v9.9.9 $(touch injected)', '', 'v1.2.4', '1.2.3'):
        bad = _run(root, 'guard', event=event, tag=tag)
        assert bad.returncode != 0 and 'tag' in bad.stderr and 'pyproject.toml version 1.2.3' in bad.stderr, tag
    assert not (root / 'injected').exists()


def test_a_main_push_rehearses_at_the_version_s_own_tag(tmp_path):
    """A push to main runs the build jobs as a rehearsal: there is no tag to
    hold to the version, so the guard answers the version's own."""
    root = _tree(tmp_path)
    rehearsal = _run(root, 'guard', event='push', tag='main', ref_type='branch')
    assert rehearsal.returncode == 0 and 'version 1.2.3; tag v1.2.3' in rehearsal.stdout
    # A branch named like a wrong tag still rehearses; a tag push is still held.
    assert _run(root, 'guard', event='push', tag='v9.9.9', ref_type='branch').returncode == 0
    assert _run(root, 'guard', event='push', tag='v9.9.9', ref_type='tag').returncode != 0


@pytest.mark.parametrize('event', ['release', 'schedule', ''])
def test_guard_refuses_any_other_event(tmp_path, event):
    result = _run(_tree(tmp_path), 'guard', event=event)
    assert result.returncode != 0 and 'unsupported release event' in result.stderr


@pytest.mark.parametrize('version', ['"1.2.3; touch injected"', '""', '123'])
def test_guard_refuses_a_version_that_is_not_a_version(tmp_path, version):
    root = _tree(tmp_path)
    (root / 'pyproject.toml').write_text(f'[project]\nname="collimate"\nversion={version}\n')
    refused = _run(root, 'guard')
    assert refused.returncode != 0 and 'pyproject.toml' in refused.stderr
    assert not (root / 'injected').exists()


PUBLISH_IF = "${{ !github.event.repository.private && github.event_name == 'push' && github.ref_type == 'tag' }}"


def _release():
    return yaml.safe_load((ROOT / '.github/workflows/release.yml').read_text())


def _needs(job):
    needs = job.get('needs', [])
    return [needs] if isinstance(needs, str) else needs


def _ancestors(jobs, name):
    seen, todo = set(), list(_needs(jobs[name]))
    while todo:
        need = todo.pop()
        if need not in seen:
            seen.add(need)
            todo.extend(_needs(jobs[need]))
    return seen


def test_actual_workflows_guard_before_build_and_publish_only_release_F1056_F1865():
    """release.yml: the guard precedes every job; everything is built before
    anything is published; and only a tag push on the public repo publishes
    (a main push, a dispatch rehearsal and the private repo build only)."""
    workflow = _release()
    jobs = workflow['jobs']
    assert set(workflow[True]) == {'push', 'workflow_dispatch'}  # YAML 1.1 reads `on` as True
    assert workflow[True]['push'] == {'tags': ['v*'], 'branches': ['main']}
    assert workflow[True]['workflow_dispatch']['inputs']['tag']['required'] is True
    assert workflow['permissions'] == {'contents': 'read'}
    assert any('release_policy.py guard' in step.get('run', '') for step in jobs['guard']['steps'])
    for name in jobs:
        if name != 'guard':
            assert 'guard' in _ancestors(jobs, name), name
    assert set(jobs) == {'guard', 'dist', 'desktop', 'assets', 'publish', 'release'}
    assert {'guard', 'dist'} <= set(_needs(jobs['desktop']))
    assert jobs['dist']['uses'] == './.github/workflows/native-wheels.yml'
    publishing = {'publish', 'release'}
    builds = set(jobs) - publishing
    assert builds == {'guard', 'dist', 'desktop', 'assets'}
    assert _ancestors(jobs, 'publish') == builds
    assert _ancestors(jobs, 'release') == builds | {'publish'}
    for name in builds:
        assert 'if' not in jobs[name], name
    for name in publishing:
        assert jobs[name]['if'] == PUBLISH_IF, name
    # No step of a build job writes anything outside the run.
    for name in builds - {'dist'}:
        for step in jobs[name]['steps']:
            assert 'gh-action-pypi-publish' not in step.get('uses', '') and 'gh release' not in step.get('run', ''), name
    # Content travels inside the server the app carries: no release asset holds it.
    assert 'content.zip' not in (ROOT / '.github/workflows/release.yml').read_text()


def test_one_publish_job_with_its_own_oidc_token_and_last_step_the_upload():
    """The one trusted-publisher record serves the one project only as a
    top-level job in release.yml, environment pypi, with a job-level id-token
    and the action's defaults (attestations on, skip-existing off); the
    release needs it."""
    jobs = _release()['jobs']
    assert 'publish' in _needs(jobs['release'])
    job = jobs['publish']
    assert job['environment'] == {'name': 'pypi', 'url': 'https://pypi.org/p/collimate'}
    assert job['permissions']['id-token'] == 'write' and job['permissions'].get('contents') != 'write'
    uploads = [step for step in job['steps'] if 'gh-action-pypi-publish' in step.get('uses', '')]
    assert uploads == [job['steps'][-1]] and uploads[0] == {
        'name': 'Publish to PyPI', 'uses': 'pypa/gh-action-pypi-publish@release/v1'}
    download = next(step for step in job['steps'] if step.get('uses') == 'actions/download-artifact@v8')
    assert download['with'] == {'name': 'native-dist', 'path': 'dist'}  # the action's default packages-dir
    verify = next(step for step in job['steps'] if 'check-wheel' in step.get('run', ''))
    assert job['steps'].index(verify) < job['steps'].index(uploads[0])
    assert 'collimate-sdk' not in (ROOT / '.github/workflows/release.yml').read_text()
    workflows = (ROOT / '.github/workflows')
    for path in workflows.glob('*.yml'):
        if path.name != 'release.yml':
            assert 'gh-action-pypi-publish' not in path.read_text(), path


def test_release_is_a_draft_until_its_feed_is_up_then_latest():
    """Latest changes only once the feed exists: the release is created as a
    draft, the binaries go up, then the three feed files, and only the
    publish step makes it Latest (PyPI is published before the release job
    runs, and the desktop no longer installs from it)."""
    job = _release()['jobs']['release']
    assert job['permissions'] == {'contents': 'write'}
    steps = job['steps']
    runs = [step.get('run', '') for step in steps]
    create = next(i for i, run in enumerate(runs) if 'gh release create' in run)
    binaries = next(i for i, step in enumerate(steps) if step.get('name') == 'Upload the binaries')
    feed = next(i for i, step in enumerate(steps) if step.get('name') == 'Upload the update feed')
    publish = next(i for i, run in enumerate(runs) if 'gh release edit' in run)
    assert create < binaries < feed < publish < len(steps)
    assert '--draft --verify-tag' in runs[create] and '--latest' not in runs[create]
    assert runs[publish].strip() == 'gh release edit "$TAG" --draft=false --latest'
    assert all('--latest' not in run and '--draft=false' not in run for i, run in enumerate(runs) if i != publish)
    assert runs[feed].split() == ['set', '-euo', 'pipefail', 'gh', 'release', 'upload', '"$TAG"', '--clobber',
                                  '--', '\\', 'latest.yml', 'latest-mac.yml', 'latest-linux.yml']
    uploaded = runs[binaries].split()
    assert 'latest*.yml' not in uploaded and not any(word.startswith('latest') for word in uploaded)
    assert {'*.dmg', '*.zip', '*.AppImage', '*.deb', '*.exe', '*.blockmap'} <= set(uploaded)
    assert not any('pip download' in run or 'collimate==$VERSION' in run for run in runs)
    assert not any('PyPI serves' in step.get('name', '') for step in steps)
    assert 'releases/latest' in runs[publish + 1]
    # No other job touches a release.
    for name, other in _release()['jobs'].items():
        if name != 'release':
            assert not any('gh release' in step.get('run', '') for step in other.get('steps', [])), name


def _release_record(tag, assets=('latest.yml', 'latest-mac.yml', 'latest-linux.yml'),
                    draft=False, prerelease=False):
    return {'tag_name': tag, 'draft': draft, 'prerelease': prerelease,
            'assets': [{'name': name} for name in assets]}


def _latest(tmp_path, releases, latest, *, gh_log=None):
    (tmp_path / 'releases.json').write_text('\n'.join(json.dumps(release) for release in releases))
    (tmp_path / 'latest.json').write_text(json.dumps(latest, indent=2) if latest else '')
    env = dict(os.environ)
    command = [sys.executable, str(SCRIPT), 'latest', '--releases', str(tmp_path / 'releases.json'),
               '--latest', str(tmp_path / 'latest.json')]
    if gh_log is not None:
        bin_dir = tmp_path / 'bin'
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / 'gh').write_text(f'#!/bin/sh\necho "$*" >> {gh_log}\n')
        (bin_dir / 'gh').chmod(0o755)
        env['PATH'] = f'{bin_dir}{os.pathsep}{env["PATH"]}'
        command.append('--remark')
    return subprocess.run(command, env=env, capture_output=True, text=True)


def test_latest_holds_for_a_complete_app_release_and_for_no_release_yet(tmp_path):
    good = _release_record('v1.2.3')
    snapshot = _release_record('snapshot-20261026', assets=('bodies.tar',), prerelease=True)
    result = _latest(tmp_path, [good, snapshot, _release_record('v1.2.4', draft=True)], good)
    assert result.returncode == 0 and 'Latest is v1.2.3' in result.stdout, result.stdout
    assert _latest(tmp_path, [], None).returncode == 0


@pytest.mark.parametrize('latest, reason', [
    (_release_record('v1.2.4', assets=('latest.yml', 'latest-mac.yml')), 'lacks latest-linux.yml'),
    (_release_record('v1.2.4', assets=('latest.yml', 'latest-linux.yml')), 'lacks latest-mac.yml'),
    (_release_record('nightly'), 'Latest is nightly'),
    (None, 'no release is Latest'),
])
def test_latest_wrong_remarks_the_newest_complete_app_release_and_fails(tmp_path, latest, reason):
    releases = [_release_record('v1.2.3'), _release_record('v1.10.0'), _release_record('v1.9.0')]
    releases += [latest] if latest else []
    log = tmp_path / 'gh.log'
    result = _latest(tmp_path, releases, latest, gh_log=log)
    assert result.returncode == 1 and reason in result.stdout, result.stdout
    # Ordered by version, not by text or by date: v1.10.0, never v1.9.0.
    assert log.read_text() == 'release edit v1.10.0 --latest\n'
    assert '::error::marked v1.10.0 Latest' in result.stdout


def test_latest_wrong_with_no_complete_release_fails_without_remarking(tmp_path):
    latest = _release_record('v1.2.3', assets=('latest.yml',))
    log = tmp_path / 'gh.log'
    result = _latest(tmp_path, [latest], latest, gh_log=log)
    assert result.returncode == 1 and 'no complete app release exists' in result.stdout
    assert not log.exists()


@pytest.mark.parametrize('release, reason', [
    (_release_record('snapshot-20261026', assets=()), 'is a full release'),
    (_release_record('snapshot-20261026', assets=('latest-mac.yml',), prerelease=True), 'carries latest-mac.yml'),
    (_release_record('snapshot-20261026', assets=('latest.yml',), draft=True), 'carries latest.yml'),
    (_release_record('v1.2.4-rc.1', assets=(), prerelease=True), 'reads as a version'),
    (_release_record('content/2.0.0', assets=(), prerelease=True), 'reads as a version'),
])
def test_a_release_that_is_not_the_app_must_be_a_feedless_prerelease_with_a_non_semver_tag(tmp_path, release, reason):
    good = _release_record('v1.2.3')
    log = tmp_path / 'gh.log'
    result = _latest(tmp_path, [good, release], good, gh_log=log)
    assert result.returncode == 1 and reason in result.stdout, result.stdout
    assert not log.exists()  # Latest itself is right; nothing to re-mark


def test_release_guard_workflow_runs_the_check_on_every_release_change_and_daily():
    workflow = yaml.safe_load((ROOT / '.github/workflows/release-guard.yml').read_text())
    triggers = workflow[True]
    assert triggers['release']['types'] == ['published', 'released', 'edited', 'prereleased', 'deleted']
    assert triggers['schedule'] and 'workflow_dispatch' in triggers
    [job] = workflow['jobs'].values()
    assert job['if'] == "${{ github.repository == 'mike-droptables/collimate' }}"
    assert job['permissions'] == {'contents': 'write'}
    run = job['steps'][-1]['run']
    assert 'releases/latest' in run and 'release_policy.py latest' in run and '--remark' in run


SDK = 'collimate-run-sdk'
WHEEL_FILES = {
    f'{SDK}.pth': f'{SDK}\n'.encode(),
    f'{SDK}/collimate/__init__.py': b'fixture', f'{SDK}/collimate/shared/docs.md': b'fixture',
    f'{SDK}/collimate/native_verifier/engine.bin': b'fixture', f'{SDK}/gen/__init__.py': b'fixture',
    'collimate_server/__init__.py': b'fixture', 'collimate_server/__main__.py': b'fixture',
    'collimate_server/main.py': b'fixture', 'collimate_server/builtins.json': b'[]',
    'collimate_server/routes/__init__.py': b'fixture',
    'collimate_server/process_manager/runner_image/Dockerfile': b'fixture',
    'collimate_server/process_manager/base_image/Dockerfile': b'fixture',
    'collimate_server/static/index.html': b'fixture',
    'collimate_content/generators/cell/entry.py': b'print(1)',
    'collimate_content/renderers/cell/view/view.renderer': b'id: view',
    'collimate_content/suggested/standard.json': b'{}',
    'collimate_builtins/text/text/text.renderer': b'id: text',
}
ENTRY_POINTS = ('[console_scripts]\ncollimate = collimate_server.main:cli_main\n'
                'collimate-lint = collimate.lint.cli:main\n')


def _wheel(tmp_path, name='collimate', version='1.2.3', changed=False, mirror_index=None, drop=(), extra=(),
           entry_points=ENTRY_POINTS):
    """A fixture collimate wheel in the one-distribution layout: the server
    package, the UI and the three bundled trees at the top level, the SDK
    packages in their own home beside them with the .pth naming it.
    ``drop`` leaves out every entry under a prefix, ``extra`` adds entries,
    ``changed`` corrupts the snapshot module."""
    import hashlib
    path = tmp_path / 'collimate-1.2.3-cp312-abi3-manylinux_2_28_x86_64.whl'
    module = b'export const mirrored = 1;'
    module_hash = hashlib.sha256(module).hexdigest()
    if mirror_index is None:
        mirror_index = {'entries': {'https://esm.sh/a@1.0.0': {'hash': module_hash}}}
    files = dict(WHEEL_FILES)
    files['collimate_module_snapshot/index.json'] = json.dumps(mirror_index).encode()
    files[f'collimate_module_snapshot/modules/{module_hash}'] = b'corrupt' if changed else module
    files.update(dict.fromkeys(extra, b'fixture'))
    with zipfile.ZipFile(path, 'w') as wheel:
        wheel.writestr(f'{name}-{version}.dist-info/METADATA',
                       f'Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n')
        if entry_points is not None:
            wheel.writestr(f'{name}-{version}.dist-info/entry_points.txt', entry_points)
        for entry, data in files.items():
            if not any(entry.startswith(prefix) for prefix in drop):
                wheel.writestr(entry, data)
    return path


def test_wheel_checker_accepts_the_one_distribution_and_refuses_a_tampered_one_F1055_F1101_F1865(tmp_path):
    root = _tree(tmp_path)
    wheel = _wheel(tmp_path)
    assert _run(root, 'check-wheel', '--wheel', str(wheel)).returncode == 0
    _wheel(tmp_path, changed=True)
    result = _run(root, 'check-wheel', '--wheel', str(wheel))
    assert result.returncode != 0 and 'module mirror bytes differ' in result.stderr


@pytest.mark.parametrize('name, version', [('collimate-sdk', '1.2.3'), ('collimate', '1.2.4')])
def test_wheel_checker_refuses_the_wrong_name_or_version(tmp_path, name, version):
    root = _tree(tmp_path)
    wheel = _wheel(tmp_path, name=name, version=version)
    result = _run(root, 'check-wheel', '--wheel', str(wheel))
    assert result.returncode != 0 and 'METADATA' in result.stderr


@pytest.mark.parametrize('prefix', [
    f'{SDK}/collimate/__init__.py', f'{SDK}/collimate/shared/', f'{SDK}/collimate/native_verifier/',
    f'{SDK}/gen/__init__.py', 'collimate_server/__init__.py', 'collimate_server/__main__.py', 'collimate_server/main.py',
    'collimate_server/builtins.json', 'collimate_server/routes/',
    'collimate_server/process_manager/runner_image/Dockerfile',
    'collimate_server/process_manager/base_image/Dockerfile', 'collimate_server/static/index.html',
    'collimate_content/generators/', 'collimate_content/renderers/', 'collimate_content/suggested/',
    'collimate_builtins/', 'collimate_module_snapshot/index.json'])
def test_wheel_checker_requires_the_sdk_the_server_ui_and_bundled_trees(tmp_path, prefix):
    root = _tree(tmp_path)
    wheel = _wheel(tmp_path, drop=(prefix,))
    result = _run(root, 'check-wheel', '--wheel', str(wheel))
    assert result.returncode != 0 and f'wheel missing {prefix}' in result.stderr


@pytest.mark.parametrize('name', ['templates/renderers/cell/view/view.renderer', 'generators/cell/entry.py',
                                  'renderers/cell/view/view.renderer', 'builtin/text/text.renderer',
                                  'content/suggested/standard.json', 'api/collimate_server/main.py',
                                  'builtins/text/text/text.renderer', 'module-snapshot/index.json',
                                  'collimate_sdk/__init__.py', 'collimate_sdk-1.2.3.dist-info/RECORD',
                                  'collimate/__init__.py', 'gen/__init__.py', 'pydantic/__init__.py',
                                  'other.pth'])
def test_wheel_checker_refuses_a_stray_top_level_entry(tmp_path, name):
    root = _tree(tmp_path)
    wheel = _wheel(tmp_path, extra=(name,))
    result = _run(root, 'check-wheel', '--wheel', str(wheel))
    assert result.returncode != 0
    assert name.split('/', 1)[0] + ' at its top level' in result.stderr or 'METADATA' in result.stderr


@pytest.mark.parametrize('name', [f'{SDK}/collimate/x.py', f'{SDK}/pydantic/__init__.py', f'{SDK}/stray.py'])
def test_wheel_checker_refuses_anything_but_the_sdk_packages_in_the_sdk_home(tmp_path, name):
    """A generator run's PYTHONPATH is the SDK's home, so a package beside
    collimate and gen there would shadow the run's own dependencies."""
    root = _tree(tmp_path)
    wheel = _wheel(tmp_path, extra=(name,))
    result = _run(root, 'check-wheel', '--wheel', str(wheel))
    if name.startswith(f'{SDK}/collimate/'):
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert f"{name.split('/')[1]} in {SDK}/; only collimate, gen belong there" in result.stderr


@pytest.mark.parametrize('pth', [None, b'', b'collimate-run-sdk\nsite-packages\n', b'collimate\n'])
def test_wheel_checker_requires_the_pth_naming_the_sdk_home_alone(tmp_path, pth):
    root = _tree(tmp_path)
    wheel = _wheel(tmp_path, drop=(f'{SDK}.pth',))
    if pth is not None:
        with zipfile.ZipFile(wheel, 'a') as archive:
            archive.writestr(f'{SDK}.pth', pth)
    result = _run(root, 'check-wheel', '--wheel', str(wheel))
    assert result.returncode != 0 and f'wheel must hold {SDK}.pth naming {SDK} and nothing else' in result.stderr


@pytest.mark.parametrize('name', [f'{SDK}/collimate/__pycache__/x.cpython-312.pyc', f'{SDK}/gen/stale.pyc',
                                  'collimate_server/static/node_modules/a.js'])
def test_wheel_checker_refuses_build_leftovers(tmp_path, name):
    root = _tree(tmp_path)
    wheel = _wheel(tmp_path, extra=(name,))
    result = _run(root, 'check-wheel', '--wheel', str(wheel))
    assert result.returncode != 0 and 'build leftover' in result.stderr


@pytest.mark.parametrize('entry_points', [
    '[console_scripts]\ncollimate = collimate_server.main:cli_main\n',
    '[console_scripts]\ncollimate = collimate_server.main:cli_main\ncollimate-lint = collimate.lint.cli:main\n'
    'collimate-extra = collimate.x:y\n',
    '[console_scripts]\ncollimate = main:cli_main\ncollimate-lint = collimate.lint.cli:main\n',
    '[console_scripts]\ncollimate-sdk-lint = collimate.lint.cli:main\ncollimate = collimate_server.main:cli_main\n',
])
def test_wheel_checker_refuses_the_wrong_console_scripts(tmp_path, entry_points):
    root = _tree(tmp_path)
    wheel = _wheel(tmp_path, entry_points=entry_points)
    result = _run(root, 'check-wheel', '--wheel', str(wheel))
    assert result.returncode != 0 and 'console scripts' in result.stderr


def test_wheel_checker_verifies_the_module_mirror_snapshot(tmp_path):
    root = _tree(tmp_path)
    for index, reason in (({'entries': {}}, 'no module mirror snapshot'),
                          ({'entries': {'https://esm.sh/a@1.0.0': {'hash': '0' * 64}}}, 'module mirror bytes differ')):
        wheel = _wheel(tmp_path, mirror_index=index)
        result = _run(root, 'check-wheel', '--wheel', str(wheel))
        assert result.returncode != 0 and reason in result.stderr


def test_installed_ownership_requires_running_module_path_F1055_map8(tmp_path, monkeypatch):
    import importlib.metadata
    from types import SimpleNamespace
    import collimate_server.bundled_integrity as bundled_integrity
    installed = tmp_path / 'site-packages/collimate_server/main.py'
    installed.parent.mkdir(parents=True)
    installed.write_text('# installed')
    monkeypatch.setattr(importlib.metadata, 'distribution', lambda name: SimpleNamespace(
        locate_file=lambda name: installed.parents[1] / name))
    assert bundled_integrity.is_packaged(str(installed))
    assert not bundled_integrity.is_packaged(str(tmp_path / 'source/api/collimate_server/main.py'))


@pytest.mark.parametrize('status', ['checking', 'checkout', 'drift: 1 files differ from the collimate RECORD',
                                    'verify_error: boom'])
def test_packaged_readiness_fails_closed_but_dev_warning_remains_F1055_map8(api_app, monkeypatch, status):
    from fastapi.testclient import TestClient
    import collimate_server.bundled_integrity as bundled_integrity
    import collimate_server.main as main
    # No lifespan: this exercises the actual readiness route with explicitly
    # selected completed/pending diagnostics, without a startup worker race.
    client = TestClient(api_app)
    monkeypatch.setattr(main, '_readiness_diagnostics', {'bundled': status})
    monkeypatch.setattr(bundled_integrity, 'is_packaged', lambda path: True)
    assert client.get('/ready').status_code == 503
    monkeypatch.setattr(main, '_readiness_diagnostics', {'bundled': 'ok'})
    assert client.get('/ready').status_code == 200
    monkeypatch.setattr(bundled_integrity, 'is_packaged', lambda path: False)
    monkeypatch.setattr(main, '_readiness_diagnostics', {'bundled': status})
    ready = client.get('/ready')
    assert ready.status_code == 200 and ready.json()['checks']['bundled'] == status
    client.close()


def _mac_part(arch, version='1.2.3'):
    return yaml.safe_dump({
        'version': version,
        'files': [{'url': f'Collimate-{version}-mac-{arch}.zip', 'sha512': f'zip-{arch}', 'size': 10},
                  {'url': f'Collimate-{version}-mac-{arch}.dmg', 'sha512': f'dmg-{arch}', 'size': 20}],
        'path': f'Collimate-{version}-mac-{arch}.zip', 'sha512': f'zip-{arch}', 'releaseDate': f'2026-11-04T0{len(arch)}:00:00.000Z',
    }, sort_keys=False)


def _policy():
    spec = importlib.util.spec_from_file_location('release_policy', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_merge_mac_feed_joins_the_two_legs_files_under_the_first_legs_fields():
    x64, arm64 = _mac_part('x64'), _mac_part('arm64')
    merged = yaml.safe_load(_policy().merge_mac_feed([x64, arm64]))
    first = yaml.safe_load(x64)
    assert merged['version'] == '1.2.3'
    assert [entry['url'] for entry in merged['files']] == [
        'Collimate-1.2.3-mac-x64.zip', 'Collimate-1.2.3-mac-x64.dmg',
        'Collimate-1.2.3-mac-arm64.zip', 'Collimate-1.2.3-mac-arm64.dmg']
    assert merged['files'][3] == {'url': 'Collimate-1.2.3-mac-arm64.dmg', 'sha512': 'dmg-arm64', 'size': 20}
    for key in ('path', 'sha512', 'releaseDate'):
        assert merged[key] == first[key]


@pytest.mark.parametrize('parts, reason', [
    ([_mac_part('x64'), _mac_part('arm64', version='1.2.4')], 'different versions'),
    ([_mac_part('x64'), _mac_part('x64')], 'a file twice'),
    ([_mac_part('x64')], 'two or more'),
])
def test_merge_mac_feed_refuses_parts_that_are_not_one_versions_two_legs(parts, reason):
    with pytest.raises(ValueError, match=reason):
        _policy().merge_mac_feed(parts)


def test_merge_mac_feed_command_writes_the_merged_feed(tmp_path):
    for arch in ('x64', 'arm64'):
        (tmp_path / f'{arch}.yml').write_text(_mac_part(arch))
    out = tmp_path / 'assets/latest-mac.yml'
    out.parent.mkdir()
    result = subprocess.run([sys.executable, str(SCRIPT), 'merge-mac-feed', '--out', str(out),
                             str(tmp_path / 'x64.yml'), str(tmp_path / 'arm64.yml')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert len(yaml.safe_load(out.read_text())['files']) == 4
    refused = subprocess.run([sys.executable, str(SCRIPT), 'merge-mac-feed', '--out', str(tmp_path / 'one.yml'),
                              str(tmp_path / 'x64.yml')], capture_output=True, text=True)
    assert refused.returncode == 1 and 'two or more' in refused.stderr
    assert not (tmp_path / 'one.yml').exists()
