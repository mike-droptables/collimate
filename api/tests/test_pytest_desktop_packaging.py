"""Every desktop package is made one way, and signs exactly when the signing
secrets are set.

build/desktop_packaging.py's ``config`` writes the packaging config
build/build_desktop.sh packages with, for a developer's build and for each
release leg (release.yml runs the script with ``--release``): the build
stamp every package carries, the server environment it carries on mac and
linux, the update feed only for a release, and signing only for a release
whose secrets are set. The committed desktop/electron-builder.yml describes
the unsigned build and holds no placeholder.
"""
from __future__ import annotations

import importlib.util
import json
import platform
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'build' / 'desktop_packaging.py'
AZURE = {'AZURE_SIGN_PUBLISHER_NAME': 'Collimate Contributors', 'AZURE_SIGN_ENDPOINT': 'https://eus.codesigning.azure.net',
         'AZURE_SIGN_ACCOUNT_NAME': 'collimate', 'AZURE_SIGN_CERTIFICATE_PROFILE': 'collimate-release'}
HEAD = '0123456789abcdef0123456789abcdef01234567'
DEVELOPER_ID = {'identity': '0123456789ABCDEF0123456789ABCDEF01234567',
                'name': 'Developer ID Application: Collimate Contributors (TEAM123456)', 'hardened_runtime': True}
ADHOC = {'identity': '-', 'name': None, 'hardened_runtime': False}
BUILT_AT = '2026-10-31T22:00:00Z'
VERSION = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']


def _load():
    spec = importlib.util.spec_from_file_location('desktop_packaging', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


packaging = _load()
server_env = sys.modules['server_env']


def environment(root: Path, target: str = 'linux-x64', version: str = VERSION, *, mach_o: bool = False,
                codesign: dict | None = None) -> Path:
    """A server environment as build/server_env.py writes one: a tree and the
    manifest naming its hash; a darwin one names who signed its Mach-O files
    (ad-hoc unless ``codesign`` says otherwise)."""
    (root / 'bin').mkdir(parents=True)
    interpreter = root / 'bin' / 'python3.13'
    interpreter.write_bytes((b'\xcf\xfa\xed\xfe' if mach_o else b'\x7fELF') + b' interpreter')
    interpreter.chmod(0o755)
    (root / 'bin' / 'python3').symlink_to('python3.13')
    uv = root / 'bin' / 'uv'
    uv.write_bytes((b'\xcf\xfa\xed\xfe' if mach_o else b'\x7fELF') + b' uv')
    uv.chmod(0o755)
    lib = root / 'lib' / 'python3.13' / 'site-packages'
    (lib / 'collimate_server' / '__pycache__').mkdir(parents=True)
    (lib / 'collimate_server' / 'main.py').write_text('app = 1\n')
    (lib / 'collimate_server' / '__pycache__' / 'main.cpython-313.pyc').write_bytes(b'\xf3\r\r\n bytecode')
    if mach_o:
        (lib / '_quickjs.abi3.so').write_bytes(b'\xca\xfe\xba\xbe universal')
    manifest = {'schema': server_env.SCHEMA, 'target': target, 'collimate': {'version': version},
                'uv': {'version': server_env.UV_VERSION}, 'hash': server_env.tree_hash(root)}
    if target.startswith('darwin-'):
        manifest['codesign'] = codesign or ADHOC
    (root / server_env.MANIFEST).write_text(json.dumps(manifest))
    return root


def config(platform_name, env=None, *, dirty=False, server=None, release=False):
    return packaging.packaging_config(platform_name, env or {}, commit=HEAD, dirty=dirty, built_at=BUILT_AT,
                                      server=server, release=release)


def test_every_build_extends_the_committed_config_and_carries_the_stamp_and_its_server(tmp_path):
    server = environment(tmp_path / 'server')
    built = config('linux', dirty=True, server=server)
    digest = json.loads((server / server_env.MANIFEST).read_text())['hash']
    assert built == {
        'extends': './electron-builder.yml',
        'extraResources': [{'from': str(server.resolve()), 'to': 'server'}],
        # The one version is pyproject.toml's, stamped here: desktop/package.json has none.
        'extraMetadata': {'version': VERSION, 'collimate': {'signed': False, 'build': {
            'commit': HEAD, 'dirty': True, 'builtAt': BUILT_AT, 'server': digest}}},
        # Not a release: no update feed, neither app-update.yml nor latest*.yml.
        'publish': None,
    }
    # A release keeps the yml's feed; Windows carries no server and stamps none.
    assert 'publish' not in config('linux', server=server, release=True)
    win = config('win', release=True)
    assert win['extraMetadata']['collimate']['build']['server'] is None and 'extraResources' not in win


def test_only_a_release_signs_and_then_exactly_when_its_secrets_are_set(tmp_path):
    server = environment(tmp_path / 'server', 'darwin-arm64', mach_o=True)
    signing = {'MAC_SIGNING': 'true', 'WIN_SIGNING': 'true', **AZURE}
    unsigned_mac = {'notarize': False, 'identity': '-', 'hardenedRuntime': False}
    # Ad-hoc signed unless a release has the secrets: the hardened runtime off, which an ad-hoc signature cannot satisfy.
    for build in (config('mac', signing, server=server), config('mac', {}, server=server, release=True)):
        assert {k: v for k, v in build['mac'].items() if k != 'signIgnore'} == unsigned_mac
        assert build['extraMetadata']['collimate']['signed'] is False
    # Signed, the app is signed with the very certificate the server environment was signed with.
    developer = environment(tmp_path / 'developer', 'darwin-arm64', mach_o=True, codesign=DEVELOPER_ID)
    signed = config('mac', signing, server=developer, release=True)
    assert {k: v for k, v in signed['mac'].items() if k != 'signIgnore'} == {
        'notarize': True, 'identity': DEVELOPER_ID['identity']}
    assert signed['extraMetadata']['collimate']['signed'] is True
    win = config('win', signing, release=True)
    assert win['extraMetadata']['collimate']['signed'] is True
    assert win['win'] == {'azureSignOptions': {
        'publisherName': 'Collimate Contributors', 'endpoint': 'https://eus.codesigning.azure.net',
        'codeSigningAccountName': 'collimate', 'certificateProfileName': 'collimate-release'}}
    assert 'win' not in config('win', signing) and 'win' not in config('win', {}, release=True)
    # Linux never signs, whatever the flags say.
    linux = config('linux', signing, server=environment(tmp_path / 'linux'), release=True)
    assert linux['extraMetadata']['collimate']['signed'] is False and 'mac' not in linux


def test_the_mac_signer_leaves_the_whole_server_environment_as_it_was_signed(tmp_path):
    """build/server_env.py signed the environment's Mach-O files before it
    hashed the tree; electron-builder's signer, which walks the whole bundle
    and re-signs every file it reads as binary, must touch none of it (the
    first mac release legs: re-signed, the packaged tree no longer hashed to
    its manifest), and still signs everything else in the app."""
    server = environment(tmp_path / 'server', 'darwin-x64', mach_o=True)
    for build in (config('mac', server=server),
                  config('mac', {'MAC_SIGNING': 'true'}, release=True,
                         server=environment(tmp_path / 'developer', 'darwin-x64', mach_o=True, codesign=DEVELOPER_ID))):
        (pattern,) = build['mac']['signIgnore']
        ignore = re.compile(pattern)
        app = '/build/release/mac/Collimate.app/Contents'
        for inside in ('bin/python3.13', 'bin/uv', 'lib/python3.13/site-packages/_quickjs.abi3.so', 'bin/python3',
                       'collimate-server.json',
                       'lib/python3.13/site-packages/collimate_server/__pycache__/main.cpython-313.pyc'):
            assert ignore.search(f'{app}/Resources/server/{inside}'), inside
        for electron in (f'{app}/MacOS/Collimate', f'{app}/Frameworks/Electron Framework.framework',
                         f'{app}/Frameworks/Collimate Helper.app/Contents/MacOS/Collimate Helper',
                         f'{app}/Resources/app.asar', f'{app}/Resources/serverless/x.dylib'):
            assert not ignore.search(electron), electron


@pytest.mark.parametrize('codesign, env, release, match', [
    (ADHOC, {'MAC_SIGNING': 'true'}, True, 'signed ad-hoc'),
    (DEVELOPER_ID, {}, False, 'signed with Developer ID Application: Collimate'),
    (DEVELOPER_ID, {'MAC_SIGNING': 'false'}, True, 'ad-hoc build cannot carry'),
    ({}, {}, False, 'names no codesign identity'),
])
def test_a_mac_build_refuses_a_server_signed_otherwise_than_the_app(tmp_path, codesign, env, release, match):
    """A signed app with an ad-hoc server fails notarization, and an ad-hoc
    app with a Developer ID server is not what its stamp says: the server
    must have been signed as the app is."""
    server = environment(tmp_path / 'server', 'darwin-arm64', mach_o=True)
    manifest = json.loads((server / server_env.MANIFEST).read_text())
    manifest['codesign'] = codesign
    (server / server_env.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(packaging.Refused, match=match):
        config('mac', env, server=server, release=release)


@pytest.mark.parametrize(('platform_name', 'server_kind', 'match'), [
    ('linux', None, 'name it with --server'),
    ('mac', None, 'name it with --server'),
    ('win', 'linux-x64', 'connect-only'),
    ('mac', 'linux-x64', 'not a mac one'),
    ('linux', 'darwin-arm64', 'not a linux one'),
    ('linux', 'stale', 'hashes to'),
    ('linux', 'other-version', 'not the app'),
])
def test_a_build_refuses_a_server_it_cannot_carry(tmp_path, platform_name, server_kind, match):
    server = None
    if server_kind == 'stale':
        server = environment(tmp_path / 'server')
        (server / 'lib' / 'stray.py').write_text('')
    elif server_kind == 'other-version':
        server = environment(tmp_path / 'server', version='0.0.1')
    elif server_kind:
        server = environment(tmp_path / 'server', server_kind)
    with pytest.raises(packaging.Refused, match=match):
        config(platform_name, server=server)


@pytest.mark.parametrize('commit, built_at, match', [
    (HEAD[:10], BUILT_AT, 'commit'),
    (HEAD.upper(), BUILT_AT, 'commit'),
    (HEAD, '2026-10-31T22:00:00+00:00', 'built-at'),
    (HEAD, '2026-10-31', 'built-at'),
])
def test_a_build_refuses_a_stamp_the_shell_would_not_read(commit, built_at, match):
    with pytest.raises(packaging.Refused, match=match):
        packaging.packaging_config('win', {}, commit=commit, dirty=False, built_at=built_at, server=None, release=False)


@pytest.mark.parametrize('missing', sorted(AZURE))
def test_a_signed_windows_release_refuses_a_missing_azure_variable(missing):
    with pytest.raises(packaging.Refused, match=missing):
        config('win', {'WIN_SIGNING': 'true', **AZURE, missing: ' '}, release=True)


def test_a_flag_that_is_not_true_or_false_is_refused(tmp_path):
    with pytest.raises(packaging.Refused, match='MAC_SIGNING'):
        config('mac', {'MAC_SIGNING': 'yes'}, server=environment(tmp_path / 's', 'darwin-arm64'), release=True)


def test_the_config_command_writes_json_with_the_standard_library_only(tmp_path):
    out = tmp_path / 'electron-builder.json'
    server = environment(tmp_path / 'server')

    def run(*args, **env):
        return subprocess.run([sys.executable, '-I', str(SCRIPT), 'config', '--commit', HEAD, '--built-at', BUILT_AT,
                               '--out', str(out), *args], env={'PATH': '/usr/bin:/bin', **env},
                              capture_output=True, text=True)
    linux = run('--platform', 'linux', '--dirty', '--server', str(server))
    assert linux.returncode == 0, linux.stderr
    assert f'linux build {HEAD[:10]} (uncommitted changes), unsigned' in linux.stdout
    assert json.loads(out.read_text())['extraMetadata']['collimate']['build']['dirty'] is True
    win = run('--platform', 'win', '--clean', '--release', WIN_SIGNING='true', **AZURE)
    assert win.returncode == 0, win.stderr
    assert 'win release' in win.stdout and 'signed' in win.stdout
    assert json.loads(out.read_text())['win']['azureSignOptions']['publisherName'] == 'Collimate Contributors'
    # The tree's state is stated, never assumed, and stated once.
    assert run('--platform', 'win').returncode == 2
    assert run('--platform', 'win', '--clean', '--dirty').returncode == 2
    refused = run('--platform', 'win', '--clean', '--release', WIN_SIGNING='true')
    assert refused.returncode == 1 and 'AZURE_SIGN_PUBLISHER_NAME' in refused.stderr
    assert subprocess.run([sys.executable, '-I', str(SCRIPT), 'local'], capture_output=True).returncode == 2


def test_the_committed_config_is_the_unsigned_build_with_no_placeholder():
    text = (ROOT / 'desktop/electron-builder.yml').read_text()
    built = yaml.safe_load(text)
    assert 'TODO' not in text
    assert 'azureSignOptions' not in built['win']
    assert 'verifyUpdateCodeSignature' not in built['win']
    assert 'identity' not in built['mac']
    assert 'extraMetadata' not in built and 'extraResources' not in built


def test_target_names_mirror_the_committed_config():
    built = yaml.safe_load((ROOT / 'desktop/electron-builder.yml').read_text())
    for host, (option, names) in packaging.TARGETS.items():
        assert option == {'darwin': '--mac', 'linux': '--linux', 'win32': '--win'}[host]
        assert tuple(target['target'] for target in built[option[2:]]['target']) == names


@pytest.mark.parametrize('sys_platform, machine, unpacked, expected', [
    ('darwin', 'arm64', False, ['--mac', 'dmg:arm64', 'zip:arm64']),
    ('darwin', 'x86_64', False, ['--mac', 'dmg:x64', 'zip:x64']),
    ('linux', 'x86_64', False, ['--linux', 'AppImage:x64', 'deb:x64']),
    ('linux', 'aarch64', False, ['--linux', 'AppImage:arm64', 'deb:arm64']),
    ('win32', 'AMD64', False, ['--win', 'nsis:x64']),
    ('darwin', 'arm64', True, ['--mac', 'dir:arm64']),
    ('linux', 'AMD64', True, ['--linux', 'dir:x64']),
])
def test_a_build_packages_the_host_arch_only(sys_platform, machine, unpacked, expected):
    """The server environment and the wheel's native binding are the host
    CPU's, so another arch's artifact would carry a server that cannot run.
    The arch is narrowed on the command line, where it replaces the config's
    targets: an extending config's mac.target would be appended to the yml's."""
    host = packaging.host_platform(sys_platform), packaging.host_arch(machine)
    assert packaging.host_targets(*host, unpacked) == expected


@pytest.mark.parametrize('machine', ['i686', 'armv7l', 'ppc64le', 'riscv64', ''])
def test_a_build_refuses_a_machine_it_has_no_package_for(machine):
    with pytest.raises(packaging.Refused, match='machine'):
        packaging.host_arch(machine)


def test_a_build_refuses_an_unknown_platform():
    with pytest.raises(packaging.Refused, match='platform'):
        packaging.host_platform('freebsd14')
    with pytest.raises(packaging.Refused, match='platform'):
        packaging.host_targets('mac', 'x64', False)
    with pytest.raises(packaging.Refused, match='arch'):
        packaging.host_targets('linux', 'ia32', False)
    with pytest.raises(packaging.Refused, match='platform'):
        packaging.packaging_config('freebsd', {}, commit=HEAD, dirty=False, built_at=BUILT_AT, server=None, release=False)


def test_the_targets_command_reads_the_host():
    def targets(*flags):
        return subprocess.run([sys.executable, '-I', str(SCRIPT), 'targets', *flags],
                              env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
    host = packaging.host_platform(sys.platform), packaging.host_arch(platform.machine())
    full = targets()
    assert full.returncode == 0, full.stderr
    assert full.stdout == ' '.join(packaging.host_targets(*host, False)) + '\n'
    assert targets('--dir').stdout == ' '.join(packaging.host_targets(*host, True)) + '\n'
    assert targets('--x64').returncode == 2


# ---------------------------------------------------------------------------
# release.yml's desktop legs: build_desktop.sh --release on a runner per target
# ---------------------------------------------------------------------------

def _desktop():
    return yaml.safe_load((ROOT / '.github/workflows/release.yml').read_text())['jobs']['desktop']


def test_each_leg_packages_with_the_build_script_on_a_runner_of_its_target():
    desktop = _desktop()
    assert desktop['needs'] == ['guard', 'dist']
    legs = {leg['target']: (leg['runner'], leg['platform']) for leg in desktop['strategy']['matrix']['include']}
    assert legs == {'darwin-arm64': ('macos-15', 'mac'), 'darwin-x64': ('macos-15-intel', 'mac'),
                    'linux-x64': ('ubuntu-24.04', 'linux'), 'win-x64': ('windows-2025', 'win')}
    # The runners native-wheels.yml builds each target's wheel on.
    native = yaml.safe_load((ROOT / '.github/workflows/native-wheels.yml').read_text())['jobs']['prepare']
    runners = {leg['target']: leg['runner'] for leg in native['strategy']['matrix']['include']}
    for target, (runner, _) in legs.items():
        if target in runners:
            assert runners[target] == runner, target
    steps = {step.get('name', step.get('uses', '')): step for step in desktop['steps']}
    download = next(step for step in desktop['steps'] if step.get('uses', '').startswith('actions/download-artifact'))
    assert download['with'] == {'name': 'wheel-${{ matrix.target }}', 'path': 'candidates'}
    assert download['if'] == "${{ matrix.platform != 'win' }}"
    package = steps['Package']
    # The build script, given three tries a minute apart over electron-builder's
    # anonymous GitHub downloads (the v0.1.2 release's 504s), and nothing else.
    run = package['run']
    assert 'for attempt in 1 2 3; do' in run
    assert 'if bash build/build_desktop.sh --release "${wheel[@]}"; then break; fi' in run
    assert 'if [ "$attempt" = 3 ]; then exit 1; fi' in run and 'sleep 60' in run
    assert run.count('bash build/build_desktop.sh') == 1 and run.rstrip().endswith('done')
    assert 'electron-builder' not in run.replace('electron-builder fetches', '') and 'desktop_packaging' not in run
    assert package['env']['PYTHON'] == 'python'
    # The server environment's pinned GitHub release downloads go as the workflow, not anonymously.
    assert package['env']['GITHUB_TOKEN'] == '${{ github.token }}'
    for variable in AZURE:
        assert package['env'][variable] == '${{ vars.%s }}' % variable
    assert desktop['env'] == {'MAC_SIGNING': "${{ secrets.CSC_LINK != '' }}",
                              'WIN_SIGNING': "${{ secrets.AZURE_CLIENT_SECRET != '' }}"}
    for name in ('CSC_LINK', 'CSC_KEY_PASSWORD', 'APPLE_ID', 'APPLE_APP_SPECIFIC_PASSWORD', 'APPLE_TEAM_ID',
                 'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET'):
        assert package['env'][name] == '${{ secrets.%s }}' % name
    assert package['env']['CSC_IDENTITY_AUTO_DISCOVERY'] == '${{ env.MAC_SIGNING }}'
    mac = steps['Verify the macOS signature']['if']
    assert "env.MAC_SIGNING == 'true'" in mac and "github.event_name == 'push'" in mac
    win = steps['Verify the Windows signature']['if']
    assert "env.WIN_SIGNING == 'true'" in win and "github.event_name == 'push'" in win


def test_each_leg_with_a_server_boots_it_and_drives_the_packaged_app():
    steps = {step.get('name'): step for step in _desktop()['steps'] if 'name' in step}
    offline = steps['Boot the packaged server and run a generator with no network']
    assert offline['if'] == "${{ matrix.platform == 'linux' }}"
    assert 'build/check_server_env.py desktop/release/linux-unpacked/resources/server --offline' in offline['run']
    mac = steps['Boot the packaged server and run a generator']['run']
    # The app's seal over the server as it was signed, then the server's own hash.
    assert mac.index('codesign --verify --deep --strict') < mac.index('build/check_server_env.py "$server"')
    drive = steps['Drive the packaged app']
    assert drive['if'] == "${{ matrix.platform != 'win' }}"
    assert 'COLLIMATE_E2E_PACKAGED' in drive['run'] and 'packaged.spec.ts' in drive['run']
    assert (ROOT / 'frontend/tests/electron/packaged.spec.ts').is_file()
    names = [step.get('name') for step in _desktop()['steps']]
    assert names.index('Package') < names.index('Boot the packaged server and run a generator with no network') < names.index('Drive the packaged app')


@pytest.mark.parametrize('platform_name, mac, win, expected', [
    ('mac', 'true', 'true', {'CSC_LINK', 'CSC_KEY_PASSWORD', 'APPLE_ID', 'APPLE_APP_SPECIFIC_PASSWORD', 'APPLE_TEAM_ID'}),
    ('mac', 'false', 'true', set()),
    ('win', 'true', 'true', {'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET'}),
    ('win', 'true', 'false', set()),
    ('linux', 'true', 'true', set()),
])
def test_the_package_step_gives_a_leg_only_its_own_platforms_signing_inputs(tmp_path, platform_name, mac, win, expected):
    """An unset secret renders as '' and electron-builder reads an empty
    CSC_LINK as a certificate path; CSC_LINK is also the Windows signtool
    fallback. So a leg sees only its own platform's inputs, and only when
    that platform signs: the others are unset, not empty. A leg with a
    server hands the build script its one wheel."""
    package = next(step for step in _desktop()['steps'] if step.get('name') == 'Package')
    (tmp_path / 'build').mkdir()
    (tmp_path / 'build' / 'build_desktop.sh').write_text('printf "%s\\n" "$@" > "$RECORD.args"\nenv > "$RECORD.env"\n')
    (tmp_path / 'candidates').mkdir()
    (tmp_path / 'candidates' / 'collimate-0.1.0-cp312-abi3-macosx_11_0_arm64.whl').write_text('wheel')
    secrets = {name: 'x' for name in ('CSC_LINK', 'CSC_KEY_PASSWORD', 'APPLE_ID', 'APPLE_APP_SPECIFIC_PASSWORD',
                                      'APPLE_TEAM_ID', 'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET')}
    env = {'PATH': '/usr/bin:/bin', 'RECORD': str(tmp_path / 'record'), 'PLATFORM': platform_name,
           'MAC_SIGNING': mac, 'WIN_SIGNING': win, **secrets}
    result = subprocess.run(['bash', '-c', package['run']], env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    args = (tmp_path / 'record.args').read_text().split('\n')[:-1]
    assert args == (['--release'] if platform_name == 'win'
                    else ['--release', '--wheel', 'candidates/collimate-0.1.0-cp312-abi3-macosx_11_0_arm64.whl'])
    seen = {line.split('=', 1)[0] for line in (tmp_path / 'record.env').read_text().splitlines()}
    assert seen & set(secrets) == expected


def test_a_leg_with_a_server_refuses_anything_but_one_wheel(tmp_path):
    package = next(step for step in _desktop()['steps'] if step.get('name') == 'Package')
    (tmp_path / 'candidates').mkdir()
    result = subprocess.run(['bash', '-c', package['run']], cwd=tmp_path, capture_output=True, text=True,
                            env={'PATH': '/usr/bin:/bin', 'PLATFORM': 'linux', 'MAC_SIGNING': 'false', 'WIN_SIGNING': 'false'})
    assert result.returncode == 1 and 'expected one collimate wheel' in result.stdout


def test_the_mac_legs_set_their_feed_part_aside_and_the_assets_job_merges_them():
    workflow = yaml.safe_load((ROOT / '.github/workflows/release.yml').read_text())
    steps = _desktop()['steps']
    aside = next(step for step in steps if step.get('name') == 'Set the mac feed part aside')
    assert aside['if'] == "${{ matrix.platform == 'mac' }}" and 'mv desktop/release/latest-mac.yml' in aside['run']
    names = [step.get('name') for step in steps]
    assert names.index('Set the mac feed part aside') < names.index('Upload installers')
    upload = next(step for step in steps if step.get('with', {}).get('name') == 'mac-feed-${{ matrix.target }}')
    assert upload['with']['path'] == 'desktop/feed/latest-mac.yml'
    merge = next(step for step in workflow['jobs']['assets']['steps'] if step.get('name') == 'Merge the mac feed')
    assert 'release_policy.py merge-mac-feed --out assets/latest-mac.yml' in merge['run']
    assert merge['run'].index('darwin-x64') < merge['run'].index('darwin-arm64')
    assert any(step.get('with', {}).get('name') == 'release-mac-feed' for step in workflow['jobs']['assets']['steps'])
