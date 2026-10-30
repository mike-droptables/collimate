"""Release artifacts pin the SDK actually selected by the release build."""
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


def _sdk(root, version='"1.2.3"'):
    (root / 'sdk/pyproject.toml').write_text(f'[project]\nname="collimate-sdk"\nversion={version}\n')


def _tree(tmp_path):
    for folder in ('api', 'desktop', 'sdk'):
        (tmp_path / folder).mkdir()
    _sdk(tmp_path)
    (tmp_path / 'api/pyproject.toml').write_text(
        '[project]\nname="collimate"\nversion="1.2.3"\n'
        'dependencies = [\n    "collimate-sdk>=0.1.0",\n    "pyyaml>=6",\n]\n')
    (tmp_path / 'desktop/package.json').write_text('{"version":"1.2.3"}')
    return tmp_path


def _run(root, command, *args, event='push', tag='v1.2.3'):
    """The script under release.yml's environment: a pushed tag arrives as the
    runner's GITHUB_REF_NAME, a dispatch rehearsal's as its DISPATCH_TAG input."""
    env = {key: value for key, value in os.environ.items() if key not in ('GITHUB_REF_NAME', 'DISPATCH_TAG')}
    env.update({'EVENT_NAME': event, 'GITHUB_REF_NAME' if event == 'push' else 'DISPATCH_TAG': tag})
    return subprocess.run([sys.executable, str(SCRIPT), command, '--root', str(root), *args],
                          env=env, capture_output=True, text=True)


def test_pin_rewrites_only_dependency_to_selected_version_F1055_F1101_F1865(tmp_path):
    root = _tree(tmp_path)
    before = (root / 'api/pyproject.toml').read_text()
    result = _run(root, 'pin-sdk')
    assert result.returncode == 0, result.stderr
    assert (root / 'api/pyproject.toml').read_text() == before.replace(
        'collimate-sdk>=0.1.0', 'collimate-sdk==1.2.3')
    assert _run(root, 'pin-sdk').returncode == 0


@pytest.mark.parametrize('bad', ['""', '"1.2.3\\n1.2.4"', '"1.2.3; touch injected"', '123', '"1.2.3" "1.2.4"'])
def test_bad_pin_refused_before_manifest_write_F1055(tmp_path, bad):
    root = _tree(tmp_path)
    manifest = root / 'api/pyproject.toml'
    before = manifest.read_bytes()
    _sdk(root, bad)
    assert _run(root, 'pin-sdk').returncode != 0
    assert manifest.read_bytes() == before
    assert not (root / 'injected').exists()


@pytest.mark.parametrize('dependency', ['"pyyaml>=6",', '"collimate-sdk>=0.1.0",\n"collimate-sdk==0.2.0",'])
def test_ambiguous_or_absent_sdk_refused_without_write_F1055(tmp_path, dependency):
    root = _tree(tmp_path)
    manifest = root / 'api/pyproject.toml'
    manifest.write_text(manifest.read_text().replace('"collimate-sdk>=0.1.0",', dependency))
    before = manifest.read_bytes()
    result = _run(root, 'pin-sdk')
    assert result.returncode != 0 and 'exactly one' in result.stderr
    assert manifest.read_bytes() == before


@pytest.mark.parametrize('event', ['push', 'workflow_dispatch'])
def test_tag_guard_checks_both_versions_and_the_tag_on_either_trigger_F1056(tmp_path, event):
    """A pushed tag and a rehearsal's tag input are held to the one version
    alike; nothing else starts a release."""
    root = _tree(tmp_path)
    assert _run(root, 'guard', event=event).returncode == 0
    for tag in ('v9.9.9 $(touch injected)', '', 'v1.2.4', '1.2.3'):
        bad = _run(root, 'guard', event=event, tag=tag)
        assert bad.returncode != 0 and 'tag' in bad.stderr, tag
    assert not (root / 'injected').exists()
    (root / 'desktop/package.json').write_text('{"version":"1.2.4"}')
    mismatch = _run(root, 'guard', event=event)
    assert mismatch.returncode != 0
    assert 'desktop/package.json' in mismatch.stderr and 'api/pyproject.toml' in mismatch.stderr


@pytest.mark.parametrize('event', ['release', 'schedule', ''])
def test_guard_refuses_any_other_event(tmp_path, event):
    result = _run(_tree(tmp_path), 'guard', event=event)
    assert result.returncode != 0 and 'unsupported release event' in result.stderr


@pytest.mark.parametrize('event, tag', [('push', 'v1.2.3'), ('workflow_dispatch', 'v1.2.3')])
def test_guard_requires_the_sdk_at_the_app_and_desktop_version(tmp_path, event, tag):
    root = _tree(tmp_path)
    _sdk(root, '"1.2.4"')
    mismatch = _run(root, 'guard', event=event, tag=tag)
    assert mismatch.returncode != 0
    assert 'sdk/pyproject.toml version 1.2.4' in mismatch.stderr and 'api/pyproject.toml' in mismatch.stderr
    _sdk(root, '"1.2.3; touch injected"')
    refused = _run(root, 'guard', event=event, tag=tag)
    assert refused.returncode != 0 and 'sdk/pyproject.toml' in refused.stderr
    assert not (root / 'injected').exists()


PUBLISH_IF = "${{ !github.event.repository.private && github.event_name == 'push' }}"


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
    """release.yml: the guard precedes every job; the app build pins the SDK
    after preparing the workspace and before building; everything is built
    before anything is published; and only a tag push on the public repo
    publishes (a dispatch rehearsal and the private repo build only)."""
    workflow = _release()
    jobs = workflow['jobs']
    assert set(workflow[True]) == {'push', 'workflow_dispatch'}  # YAML 1.1 reads `on` as True
    assert workflow[True]['push'] == {'tags': ['v*']}
    assert workflow[True]['workflow_dispatch']['inputs']['tag']['required'] is True
    assert workflow['permissions'] == {'contents': 'read'}
    assert any('release_policy.py guard' in step.get('run', '') for step in jobs['guard']['steps'])
    for name in jobs:
        if name != 'guard':
            assert 'guard' in _ancestors(jobs, name), name
    steps = jobs['app-dist']['steps']
    index = {key: next(i for i, step in enumerate(steps) if probe(step)) for key, probe in {
        'toolchain': lambda step: step.get('uses') == './.github/actions/setup-toolchain',
        'snapshot': lambda step: step.get('name') == 'Fetch the module snapshot bodies',
        'static': lambda step: 'cp -r frontend/dist api/static' in step.get('run', ''),
        'pin': lambda step: 'release_policy.py pin-sdk' in step.get('run', ''),
        'build': lambda step: step.get('name') == 'Build wheel + sdist',
        'check': lambda step: 'release_policy.py check-wheel' in step.get('run', '')}.items()}
    assert index['toolchain'] < index['snapshot'] < index['static'] < index['pin'] < index['build'] < index['check']
    assert 'python -m build' in steps[index['build']]['run'] and steps[index['build']]['working-directory'] == 'api'
    publishing = {'publish-sdk', 'publish-app', 'release'}
    builds = set(jobs) - publishing
    assert builds == {'guard', 'sdk-dist', 'app-dist', 'content-zip', 'desktop', 'assets'}
    assert _ancestors(jobs, 'publish-sdk') == builds
    for name in builds:
        assert 'if' not in jobs[name], name
    for name in publishing:
        assert jobs[name]['if'] == PUBLISH_IF, name
    # No step of a build job writes anything outside the run.
    for name in builds - {'sdk-dist'}:
        for step in jobs[name]['steps']:
            assert 'gh-action-pypi-publish' not in step.get('uses', '') and 'gh release' not in step.get('run', ''), name


def test_two_publish_jobs_sdk_then_app_each_with_its_own_oidc_token():
    """One trusted-publisher record serves both projects only as two top-level
    jobs in release.yml, environment pypi, each with a job-level id-token and
    the action's defaults (attestations on, skip-existing off); the app's
    upload needs the SDK's, and the release needs the app's."""
    jobs = _release()['jobs']
    assert _needs(jobs['publish-app']) == ['guard', 'publish-sdk']
    assert 'publish-app' in _needs(jobs['release'])
    for name, project in (('publish-sdk', 'collimate-sdk'), ('publish-app', 'collimate')):
        job = jobs[name]
        assert job['environment'] == {'name': 'pypi', 'url': f'https://pypi.org/p/{project}'}
        assert job['permissions']['id-token'] == 'write' and job['permissions'].get('contents') != 'write'
        uploads = [step for step in job['steps'] if 'gh-action-pypi-publish' in step.get('uses', '')]
        assert uploads == [job['steps'][-1]] and uploads[0] == {
            'name': 'Publish to PyPI', 'uses': 'pypa/gh-action-pypi-publish@release/v1'}
        download = next(step for step in job['steps'] if step.get('uses') == 'actions/download-artifact@v8')
        assert download['with']['path'] == 'dist'  # the action's default packages-dir
    assert next(step for step in jobs['publish-app']['steps'] if 'download-artifact' in step.get('uses', ''))[
        'with']['name'] == 'app-dist'
    upload = next(step for step in jobs['app-dist']['steps'] if step.get('uses') == 'actions/upload-artifact@v7')
    assert upload['with'] == {'name': 'app-dist', 'path': 'api/dist/', 'if-no-files-found': 'error'}
    workflows = (ROOT / '.github/workflows')
    for path in workflows.glob('*.yml'):
        if path.name != 'release.yml':
            assert 'gh-action-pypi-publish' not in path.read_text(), path


def test_release_is_a_draft_until_its_feed_is_up_then_latest():
    """Latest changes only once the feed exists: the release is created as a
    draft, the binaries and content.zip go up, then the three feed files, then
    PyPI is confirmed, and only the last step publishes it as Latest."""
    job = _release()['jobs']['release']
    assert job['permissions'] == {'contents': 'write'}
    steps = job['steps']
    runs = [step.get('run', '') for step in steps]
    create = next(i for i, run in enumerate(runs) if 'gh release create' in run)
    binaries = next(i for i, step in enumerate(steps) if step.get('name') == 'Upload the binaries and content.zip')
    feed = next(i for i, step in enumerate(steps) if step.get('name') == 'Upload the update feed')
    pypi = next(i for i, step in enumerate(steps) if step.get('name') == 'Wait until PyPI serves both projects')
    publish = next(i for i, run in enumerate(runs) if 'gh release edit' in run)
    assert create < binaries < feed < pypi < publish < len(steps)
    assert '--draft --verify-tag' in runs[create] and '--latest' not in runs[create]
    assert runs[publish].strip() == 'gh release edit "$TAG" --draft=false --latest'
    assert all('--latest' not in run and '--draft=false' not in run for i, run in enumerate(runs) if i != publish)
    assert runs[feed].split() == ['set', '-euo', 'pipefail', 'gh', 'release', 'upload', '"$TAG"', '--clobber',
                                  '--', '\\', 'latest.yml', 'latest-mac.yml', 'latest-linux.yml']
    uploaded = runs[binaries].split()
    assert 'latest*.yml' not in uploaded and not any(word.startswith('latest') for word in uploaded)
    assert {'*.dmg', '*.zip', '*.AppImage', '*.deb', '*.exe', '*.blockmap'} <= set(uploaded)
    assert 'collimate==$VERSION' in runs[pypi] and 'collimate-sdk==$VERSION' in runs[pypi]
    assert '--no-deps' not in runs[pypi]
    assert 'releases/latest' in runs[publish + 1]
    # No other job touches a release.
    for name, other in _release()['jobs'].items():
        if name != 'release':
            assert not any('gh release' in step.get('run', '') for step in other.get('steps', [])), name


def _release_record(tag, assets=('latest.yml', 'latest-mac.yml', 'latest-linux.yml', 'content.zip'),
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
    (_release_record('v1.2.4', assets=('latest.yml', 'latest-mac.yml', 'content.zip')), 'lacks latest-linux.yml'),
    (_release_record('v1.2.4', assets=('latest.yml', 'latest-mac.yml', 'latest-linux.yml')), 'lacks content.zip'),
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


def _wheel(tmp_path, requirement='collimate-sdk==1.2.3', changed=False, mirror_index=None, drop=(), extra=()):
    """A fixture app wheel: the server modules, the UI and the three bundled
    trees under their package names. ``drop`` leaves out every entry under a
    prefix, ``extra`` adds entries, ``changed`` corrupts the snapshot module."""
    import hashlib
    path = tmp_path / 'collimate-1.2.3-py3-none-any.whl'
    module = b'export const mirrored = 1;'
    module_hash = hashlib.sha256(module).hexdigest()
    if mirror_index is None:
        mirror_index = {'entries': {'https://esm.sh/a@1.0.0': {'hash': module_hash}}}
    files = {
        'main.py': b'fixture', 'builtins.json': b'[]', 'routes/__init__.py': b'fixture',
        'process_manager/runner_image/Dockerfile': b'fixture', 'static/index.html': b'fixture',
        'collimate_content/generators/cell/entry.py': b'print(1)',
        'collimate_content/renderers/cell/view/view.renderer': b'id: view',
        'collimate_content/suggested/standard.json': b'{}',
        'collimate_builtins/text/text/text.renderer': b'id: text',
        'collimate_module_snapshot/index.json': json.dumps(mirror_index).encode(),
        f'collimate_module_snapshot/modules/{module_hash}': b'corrupt' if changed else module,
    }
    files.update(dict.fromkeys(extra, b'fixture'))
    with zipfile.ZipFile(path, 'w') as wheel:
        wheel.writestr('collimate-1.2.3.dist-info/METADATA',
                       f'Metadata-Version: 2.4\nName: collimate\nVersion: 1.2.3\nRequires-Dist: {requirement}\n')
        for name, data in files.items():
            if not any(name.startswith(prefix) for prefix in drop):
                wheel.writestr(name, data)
    return path


def test_wheel_checker_refuses_floating_wrong_or_tampered_artifact_F1055_F1101_F1865(tmp_path):
    root = _tree(tmp_path)
    wheel = _wheel(tmp_path)
    assert _run(root, 'check-wheel', '--wheel', str(wheel)).returncode == 0
    for requirement in ('collimate-sdk>=1.2.3', 'collimate-sdk==1.2.4', 'pyyaml>=6',
                        'collimate-sdk==1.2.3; python_version < "3.13"',
                        'collimate-sdk==1.2.3\nRequires-Dist: collimate-sdk>=1.2.3'):
        _wheel(tmp_path, requirement)
        result = _run(root, 'check-wheel', '--wheel', str(wheel))
        assert result.returncode != 0 and 'collimate-sdk==1.2.3' in result.stderr
    _wheel(tmp_path, changed=True)
    result = _run(root, 'check-wheel', '--wheel', str(wheel))
    assert result.returncode != 0 and 'module mirror bytes differ' in result.stderr


@pytest.mark.parametrize('prefix', ['static/', 'main.py', 'builtins.json', 'routes/',
                                    'process_manager/runner_image/Dockerfile',
                                    'collimate_content/generators/', 'collimate_content/renderers/',
                                    'collimate_content/suggested/', 'collimate_builtins/',
                                    'collimate_module_snapshot/index.json'])
def test_wheel_checker_requires_the_server_ui_and_bundled_trees(tmp_path, prefix):
    root = _tree(tmp_path)
    wheel = _wheel(tmp_path, drop=(prefix,))
    result = _run(root, 'check-wheel', '--wheel', str(wheel))
    assert result.returncode != 0 and f'wheel missing {prefix}' in result.stderr


@pytest.mark.parametrize('name', ['templates/renderers/cell/view/view.renderer', 'generators/cell/entry.py',
                                  'renderers/cell/view/view.renderer', 'builtin/text/text.renderer',
                                  'collimate/__init__.py', 'content/suggested/standard.json',
                                  'builtins/text/text/text.renderer', 'module-snapshot/index.json'])
def test_wheel_checker_refuses_a_tree_outside_its_package(tmp_path, name):
    root = _tree(tmp_path)
    wheel = _wheel(tmp_path, extra=(name,))
    result = _run(root, 'check-wheel', '--wheel', str(wheel))
    assert result.returncode != 0 and 'wheel holds ' + name.split('/', 1)[0] + '/' in result.stderr


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
    import bundled_integrity
    installed = tmp_path / 'site-packages/main.py'
    installed.parent.mkdir()
    installed.write_text('# installed')
    monkeypatch.setattr(importlib.metadata, 'distribution', lambda name: SimpleNamespace(
        locate_file=lambda name: installed.parent / name))
    assert bundled_integrity.is_packaged(str(installed))
    assert not bundled_integrity.is_packaged(str(tmp_path / 'source/api/main.py'))


@pytest.mark.parametrize('status', ['checking', 'checkout', 'drift: 1 files differ from the collimate RECORD',
                                    'verify_error: boom'])
def test_packaged_readiness_fails_closed_but_dev_warning_remains_F1055_map8(api_app, monkeypatch, status):
    from fastapi.testclient import TestClient
    import bundled_integrity
    import main
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
