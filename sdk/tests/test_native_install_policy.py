"""P4F05: actual native preparation/validation/release graph preserves gates."""
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / '.github/workflows'


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def test_all_four_real_targets_and_interpreters_precede_distribution__P4F05():
    workflow = load('sdk-native-wheels.yml')
    prepare = workflow['jobs']['prepare']
    expected = {'linux-x64':'ubuntu-24.04', 'linux-arm64':'ubuntu-24.04-arm',
                'darwin-x64':'macos-15-intel', 'darwin-arm64':'macos-15'}
    assert {row['target']:row['runner'] for row in prepare['strategy']['matrix']['include']} == expected
    preparation = '\n'.join(step.get('run', '') for step in prepare['steps'])
    assert 'prepare_verifier_engine.py --target linux-x64' in preparation
    assert 'prepare_verifier_engine.py --target linux-arm64' in preparation
    install = workflow['jobs']['install']
    matrix = install['strategy']['matrix']
    assert set(matrix['target']) == set(expected)
    assert matrix['python'] == ['3.12','3.13','3.14']
    assert {row['target']:row['runner'] for row in matrix['include']} == expected
    assert install['needs'] == 'prepare'
    assert workflow['jobs']['distribution']['needs'] == 'install'
    run = '\n'.join(step.get('run','') for step in install['steps'])
    assert 'scripts/prove_native_verifier.py' in run
    assert 'NATIVE_DENIED_LOG' in run and 'test ! -e "$NATIVE_DENIED_LOG"' in run
    assert '--no-index --find-links' in run and '--no-build --from' in run
    assert not any(step.get('uses','').startswith('actions/setup-node') for step in install['steps'])


def test_publication_requires_early_version_guard_and_all_native_execution__P4F05():
    jobs = load('release.yml')['jobs']
    assert jobs['sdk-dist']['needs'] == 'guard'
    assert jobs['sdk-dist']['uses'] == './.github/workflows/sdk-native-wheels.yml'
    assert 'sdk-dist' in jobs['publish-sdk']['needs']
    # Only the public repo publishes, and only from a pushed tag; the private
    # repo and a dispatch rehearsal build every distribution and stop.
    for name in ('publish-sdk', 'publish-app', 'release'):
        assert jobs[name]['if'] == "${{ !github.event.repository.private && github.event_name == 'push' }}", name
    assert 'if' not in jobs['sdk-dist']
    steps = jobs['publish-sdk']['steps']
    verify = next(i for i,step in enumerate(steps) if step.get('name')=='Verify all four native wheels')
    publish = next(i for i,step in enumerate(steps) if step.get('name')=='Publish to PyPI')
    assert verify < publish == len(steps) - 1
    assert steps[publish] == {'name': 'Publish to PyPI', 'uses': 'pypa/gh-action-pypi-publish@release/v1'}
