"""The desktop release signs exactly when the signing secrets are set.

build/desktop_packaging.py writes each release leg's packaging config, and
release.yml's desktop job derives the two signing flags from the secrets,
packages with that config and verifies a signature only on a signed leg; the
committed desktop/electron-builder.yml describes the unsigned build and holds
no placeholder.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'build' / 'desktop_packaging.py'
AZURE = {'AZURE_SIGN_PUBLISHER_NAME': 'Collimate Contributors', 'AZURE_SIGN_ENDPOINT': 'https://eus.codesigning.azure.net',
         'AZURE_SIGN_ACCOUNT_NAME': 'collimate', 'AZURE_SIGN_CERTIFICATE_PROFILE': 'collimate-release'}


def _load():
    spec = importlib.util.spec_from_file_location('desktop_packaging', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


packaging = _load()


def test_every_leg_extends_the_committed_config_and_stamps_the_signature():
    unsigned = {'MAC_SIGNING': 'false', 'WIN_SIGNING': 'false'}
    for platform in ('mac', 'linux', 'win'):
        config = packaging.packaging_config(platform, unsigned)
        assert config['extends'] == './electron-builder.yml'
        assert config['extraMetadata'] == {'collimate': {'signed': False}}
    # Unsigned mac is ad-hoc signed, without the hardened runtime an ad-hoc
    # signature cannot satisfy; unsigned Windows adds nothing.
    assert packaging.packaging_config('mac', unsigned)['mac'] == {
        'notarize': False, 'identity': '-', 'hardenedRuntime': False}
    assert 'win' not in packaging.packaging_config('win', unsigned)
    # Unset flags are the unsigned build.
    assert packaging.packaging_config('mac', {}) == packaging.packaging_config('mac', unsigned)


def test_a_signed_mac_leg_notarizes_and_a_signed_windows_leg_verifies_updates_with_the_azure_options():
    mac = packaging.packaging_config('mac', {'MAC_SIGNING': 'true', 'WIN_SIGNING': 'false'})
    assert mac['extraMetadata'] == {'collimate': {'signed': True}}
    assert mac['mac'] == {'notarize': True}
    assert 'win' not in mac
    win = packaging.packaging_config('win', {'MAC_SIGNING': 'false', 'WIN_SIGNING': 'true', **AZURE})
    assert win['extraMetadata'] == {'collimate': {'signed': True}}
    assert win['win'] == {'azureSignOptions': {
        'publisherName': 'Collimate Contributors', 'endpoint': 'https://eus.codesigning.azure.net',
        'codeSigningAccountName': 'collimate', 'certificateProfileName': 'collimate-release'}}
    # Linux never signs, whatever the flags say.
    linux = packaging.packaging_config('linux', {'MAC_SIGNING': 'true', 'WIN_SIGNING': 'true', **AZURE})
    assert linux == {'extends': './electron-builder.yml', 'extraMetadata': {'collimate': {'signed': False}}}


@pytest.mark.parametrize('missing', sorted(AZURE))
def test_a_signed_windows_leg_refuses_a_missing_azure_variable(missing):
    env = {'WIN_SIGNING': 'true', **AZURE, missing: ' '}
    with pytest.raises(packaging.Refused, match=missing):
        packaging.packaging_config('win', env)


def test_a_flag_that_is_not_true_or_false_is_refused():
    with pytest.raises(packaging.Refused, match='MAC_SIGNING'):
        packaging.packaging_config('mac', {'MAC_SIGNING': 'yes'})


def test_the_command_writes_json_electron_builder_reads(tmp_path):
    out = tmp_path / 'release.electron-builder.json'
    result = subprocess.run([sys.executable, str(SCRIPT), '--platform', 'win', '--out', str(out)],
                            env={'PATH': '/usr/bin:/bin', 'WIN_SIGNING': 'true', **AZURE},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'win signed' in result.stdout
    assert json.loads(out.read_text())['win']['azureSignOptions']['publisherName'] == 'Collimate Contributors'
    refused = subprocess.run([sys.executable, str(SCRIPT), '--platform', 'win', '--out', str(out)],
                             env={'PATH': '/usr/bin:/bin', 'WIN_SIGNING': 'true'}, capture_output=True, text=True)
    assert refused.returncode == 1
    assert 'AZURE_SIGN_PUBLISHER_NAME' in refused.stderr


def test_the_committed_config_is_the_unsigned_build_with_no_placeholder():
    text = (ROOT / 'desktop/electron-builder.yml').read_text()
    config = yaml.safe_load(text)
    assert 'TODO' not in text
    assert 'azureSignOptions' not in config['win']
    assert 'verifyUpdateCodeSignature' not in config['win']
    assert 'identity' not in config['mac']
    assert 'extraMetadata' not in config


def test_release_yml_signs_a_leg_exactly_when_its_secrets_are_set():
    workflow = yaml.safe_load((ROOT / '.github/workflows/release.yml').read_text())
    desktop = workflow['jobs']['desktop']
    assert desktop['env'] == {'MAC_SIGNING': "${{ secrets.CSC_LINK != '' }}",
                              'WIN_SIGNING': "${{ secrets.AZURE_CLIENT_SECRET != '' }}"}
    steps = {step['name']: step for step in desktop['steps'] if 'name' in step}
    names = [step.get('name') for step in desktop['steps']]
    assert names.index('Write the packaging config') < names.index('Package')
    write = steps['Write the packaging config']
    assert 'build/desktop_packaging.py --platform "${PLATFORM#--}" --out desktop/release.electron-builder.json' in write['run']
    assert set(write['env']) == {'PLATFORM', *AZURE}
    for variable in AZURE:
        assert write['env'][variable] == '${{ vars.%s }}' % variable
    package = steps['Package']
    assert package['shell'] == 'bash'
    assert package['env']['PLATFORM'] == '${{ matrix.platform }}'
    assert package['env']['CSC_IDENTITY_AUTO_DISCOVERY'] == '${{ env.MAC_SIGNING }}'
    for name in ('CSC_LINK', 'CSC_KEY_PASSWORD', 'APPLE_ID', 'APPLE_APP_SPECIFIC_PASSWORD', 'APPLE_TEAM_ID',
                 'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET'):
        assert package['env'][name] == '${{ secrets.%s }}' % name
    assert package['run'].rstrip().endswith(
        'npx electron-builder --config release.electron-builder.json "$PLATFORM" --publish never')
    mac = steps['Verify the macOS signature']['if']
    assert "env.MAC_SIGNING == 'true'" in mac and "github.event_name == 'push'" in mac
    win = steps['Verify the Windows signature']['if']
    assert "env.WIN_SIGNING == 'true'" in win and "github.event_name == 'push'" in win
    # The desktop legs have Python for the config script.
    assert any(step.get('uses', '').startswith('actions/setup-python@') for step in desktop['steps'])


@pytest.mark.parametrize('platform, mac, win, expected', [
    ('--mac', 'true', 'true', {'CSC_LINK', 'CSC_KEY_PASSWORD', 'APPLE_ID', 'APPLE_APP_SPECIFIC_PASSWORD', 'APPLE_TEAM_ID'}),
    ('--mac', 'false', 'true', set()),
    ('--win', 'true', 'true', {'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET'}),
    ('--win', 'true', 'false', set()),
    ('--linux', 'true', 'true', set()),
])
def test_the_package_step_gives_a_leg_only_its_own_platforms_signing_inputs(tmp_path, platform, mac, win, expected):
    """An unset secret renders as '' and electron-builder reads an empty
    CSC_LINK as a certificate path; CSC_LINK is also the Windows signtool
    fallback. So a leg sees only its own platform's inputs, and only when
    that platform signs: the others are unset, not empty."""
    workflow = yaml.safe_load((ROOT / '.github/workflows/release.yml').read_text())
    package = next(step for step in workflow['jobs']['desktop']['steps'] if step.get('name') == 'Package')
    fake = tmp_path / 'bin'
    fake.mkdir()
    (fake / 'npx').write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RECORD.args"\nenv > "$RECORD.env"\n')
    (fake / 'npx').chmod(0o755)
    secrets = {name: 'x' for name in ('CSC_LINK', 'CSC_KEY_PASSWORD', 'APPLE_ID', 'APPLE_APP_SPECIFIC_PASSWORD',
                                      'APPLE_TEAM_ID', 'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET')}
    env = {'PATH': f'{fake}:/usr/bin:/bin', 'RECORD': str(tmp_path / 'record'), 'PLATFORM': platform,
           'MAC_SIGNING': mac, 'WIN_SIGNING': win, **secrets}
    result = subprocess.run(['bash', '-c', package['run']], env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / 'record.args').read_text().split('\n')[:5] == [
        'electron-builder', '--config', 'release.electron-builder.json', platform, '--publish']
    seen = {line.split('=', 1)[0] for line in (tmp_path / 'record.env').read_text().splitlines()}
    assert seen & set(secrets) == expected
