# SPDX-License-Identifier: MIT
"""The actual release tag guard and event-gated publication."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT.parent / '.github/workflows'


def test_tag_guard_executes_before_build_and_dispatch_never_publishes_F1971(tmp_path):
    workflow = yaml.safe_load((WORKFLOWS / 'release.yml').read_text())
    jobs = workflow['jobs']
    guard = next(step for step in jobs['guard']['steps'] if step.get('name') == 'Check the tag names the one version')
    assert jobs['sdk-dist']['needs'] == 'guard'
    assert jobs['sdk-dist']['uses'] == './.github/workflows/sdk-native-wheels.yml'
    assert {'guard', 'sdk-dist'} <= set(jobs['publish-sdk']['needs'])
    steps = jobs['publish-sdk']['steps']
    build_index = next(i for i, step in enumerate(steps) if step.get('name') == 'Verify all four native wheels')
    publish_index = next(i for i, step in enumerate(steps) if step.get('name') == 'Publish to PyPI')
    assert build_index < publish_index == len(steps) - 1
    # A dispatch rehearsal (or the private repo) never reaches the upload.
    assert jobs['publish-sdk']['if'] == "${{ !github.event.repository.private && github.event_name == 'push' }}"
    # Action defaults: a rebuild that produced different bytes must fail, not be skipped.
    assert 'with' not in steps[publish_index]
    # The tag reaches the script through the environment, never the script text.
    assert guard['env'] == {'EVENT_NAME': '${{ github.event_name }}', 'DISPATCH_TAG': '${{ inputs.tag }}'}
    assert '${{' not in guard['run']
    (tmp_path / 'build').mkdir()
    (tmp_path / 'sdk').mkdir()
    (tmp_path / 'api').mkdir()
    (tmp_path / 'desktop').mkdir()
    (tmp_path / 'sdk/pyproject.toml').write_text('[project]\nversion="0.8.2"\n')
    (tmp_path / 'api/pyproject.toml').write_text('[project]\nversion="0.8.2"\ndependencies=[]\n')
    (tmp_path / 'desktop/package.json').write_text(json.dumps({'version': '0.8.2'}))
    # The guard reads the members' pyproject.toml files, never the one in its cwd.
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion="0.8.3"\n')
    shutil.copyfile(ROOT.parent / 'build/release_policy.py', tmp_path / 'build/release_policy.py')
    output = tmp_path / 'github_output'
    for event, tag, valid in [('push', 'v0.8.2', True), ('push', 'v0.8.3', False),
                              ('push', 'v0.8.2 $(touch injected)', False), ('push', '', False),
                              ('workflow_dispatch', 'v0.8.2', True), ('workflow_dispatch', '', False),
                              ('release', 'v0.8.2', False)]:
        output.write_text('')
        env = {key: value for key, value in os.environ.items() if key not in ('GITHUB_REF_NAME', 'DISPATCH_TAG')}
        env.update({'EVENT_NAME': event, 'GITHUB_OUTPUT': str(output),
                    'GITHUB_REF_NAME' if event == 'push' else 'DISPATCH_TAG': tag,
                    'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']})
        result = subprocess.run(['bash', '-c', guard['run']], cwd=tmp_path, env=env, capture_output=True, text=True)
        assert (result.returncode == 0) is valid, (event, tag, result.stderr)
        if valid:
            assert output.read_text() == 'version=0.8.2\ntag=v0.8.2\n'
    assert not (tmp_path / 'injected').exists()
