"""Execute developer build scripts with isolated homes and fake build-tool seams."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / 'app'
    shutil.copytree(ROOT / 'build', root / 'build')
    for name in ('desktop', 'frontend', 'api/static'):
        (root / name).mkdir(parents=True)
    for name in ('desktop', 'frontend'):
        (root / name / 'package.json').write_text(json.dumps({'name': 'collimate-desktop', 'version': '0.2.0'}))
        (root / name / 'package-lock.json').write_text('{}')
    icons = root / 'desktop/icons'
    (icons / 'hicolor/16x16/apps').mkdir(parents=True)
    for name in ('app-icon.png', 'app-icon.svg', 'hicolor/16x16/apps/collimate.png'):
        (icons / name).write_text('fixture icon')
    home = tmp_path / 'home'
    home.mkdir()
    installed = home / '.local/share/collimate/app/resources'
    installed.mkdir(parents=True)
    (installed / 'app.asar').write_text('old app')
    settings = home / '.config/Collimate/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({'serverSpec': {'old': True}, 'keep': 7}))
    bins = tmp_path / 'bin'
    bins.mkdir()
    fake = '''#!/bin/bash
set -e
name="$(basename "$0")"
echo "$name $*" >> "$TOOLS_LOG"
case "$name" in
npm)
    if [ "$1" = ci ]; then
        [ "${FAIL_CI:-0}" = 0 ] || exit 21
        mkdir -p node_modules
    elif [ "$1" = run ]; then
        [ "${FAIL_BUILD:-0}" = 0 ] || exit 22
        mkdir -p dist
    fi ;;
npx)
    [ "${FAIL_PACKAGE:-0}" = 0 ] || exit 23
    if [ "$2" = electron-builder ]; then
        test -f "$HOME/.local/share/collimate/app/resources/app.asar"
        echo old-install-present-at-package >> "$TOOLS_LOG"
        mkdir -p release/linux-unpacked/resources
        echo new > release/linux-unpacked/resources/app.asar
        printf '#!/bin/sh\\nexit 0\\n' > release/linux-unpacked/collimate-desktop
        chmod +x release/linux-unpacked/collimate-desktop
    fi ;;
esac
'''
    for name in ('npm', 'npx', 'uv', 'kbuildsycoca6'):
        path = bins / name
        path.write_text(fake)
        path.chmod(0o755)
    node = shutil.which('node')
    assert node, 'Node is required by the developer build scripts'
    node_bin = Path(node).parent
    env = {k: v for k, v in os.environ.items() if not k.startswith(('SNAP', 'FLATPAK', 'XDG_'))}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'), XDG_DATA_HOME=str(home / '.local/share'), PATH=f'{bins}:{node_bin}:/usr/bin:/bin', TOOLS_LOG=str(tmp_path / 'tools.log'), TMPDIR=str(tmp_path))
    return root, home, settings, env


def run(checkout, *args, **flags):
    root, _, _, env = checkout
    return subprocess.run(['bash', str(root / 'build/build_desktop.sh'), *args], env=env | flags, text=True, capture_output=True, timeout=20)


def test_standalone_uninstall_retains_other_settings__F1058(checkout):
    _, home, settings, env = checkout
    result = run(checkout, '--uninstall', '--no-sudo')
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (home / '.local/share/collimate/app').exists()
    assert json.loads(settings.read_text()) == {'keep': 7}
    assert 'npm ' not in Path(env['TOOLS_LOG']).read_text() if Path(env['TOOLS_LOG']).exists() else True


def test_reset_rejects_incompatible_flags_before_touching_install__F1058(checkout):
    _, home, settings, _ = checkout
    before = settings.read_bytes()
    result = run(checkout, '--reset', '--no-install')
    assert result.returncode == 2
    assert (home / '.local/share/collimate/app/resources/app.asar').read_text() == 'old app'
    assert settings.read_bytes() == before



@pytest.mark.parametrize('failure', ['FAIL_SERVER', 'FAIL_BUILD', 'FAIL_PACKAGE'])
def test_reset_failed_build_keeps_install_and_server_spec__F1058(checkout, failure):
    root, home, settings, _ = checkout
    (root / 'desktop/node_modules').mkdir()
    before = settings.read_bytes()
    if failure == 'FAIL_SERVER':
        (root / 'build/build_server.sh').write_text('#!/bin/bash\nexit 24\n')
        result = run(checkout, '--reset', '--no-sudo')
    else:
        result = run(checkout, '--reset', '--skip', '--no-sudo', **{failure: '1'})
    assert result.returncode != 0
    assert (home / '.local/share/collimate/app/resources/app.asar').read_text() == 'old app'
    assert settings.read_bytes() == before


def test_reset_packages_before_replacing_install__F1058(checkout):
    _, home, settings, env = checkout
    result = run(checkout, '--reset', '--skip', '--no-sudo')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'old-install-present-at-package' in Path(env['TOOLS_LOG']).read_text()
    assert (home / '.local/share/collimate/app/resources/app.asar').read_text().strip() == 'new'
    assert json.loads(settings.read_text())['keep'] == 7


@pytest.mark.parametrize('script', ['build_desktop.sh', 'dev_desktop.sh', 'build_server.sh'])
def test_build_flows_refresh_stale_lock_and_reuse_current_install__F1087_F1122_F1084(checkout, script):
    root, _, _, env = checkout
    project = root / ('frontend' if script == 'build_server.sh' else 'desktop')
    if script == 'build_server.sh':
        sdk = root / 'sdk/scripts'
        sdk.mkdir(parents=True)
        (sdk / 'build_renderer_runtime.mjs').write_text('// isolated runtime builder seam')
    args = [] if script == 'build_server.sh' else ['--skip']
    if script == 'build_desktop.sh':
        args += ['--no-install', '--no-sudo']
    def execute():
        result = subprocess.run(['bash', str(root / 'build' / script), *args], env=env, text=True, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
    lock = (project / 'package-lock.json').read_bytes()
    execute()
    stamp = project / 'node_modules/.collimate-npm-ci-stamp'
    assert stamp.is_file()
    execute()
    # Compare to completed installation time, not directory mtime.
    future = stamp.stat().st_mtime_ns + 10_000_000
    os.utime(project / 'package-lock.json', ns=(future, future))
    execute()
    lines = Path(env['TOOLS_LOG']).read_text().splitlines()
    assert lines.count('npm ci') == 2
    assert not any(line.startswith('npm install') for line in lines)
    assert (project / 'package-lock.json').read_bytes() == lock


def test_setup_installs_only_the_frontend_lock_and_reuses_a_current_install__F1059(checkout):
    root, _, _, env = checkout
    shutil.copy2(ROOT / 'setup.sh', root)
    (root / 'build/install_toolchain.sh').write_text('echo "install_toolchain $*" >> "$TOOLS_LOG"\n')
    result = subprocess.run(['bash', str(root / 'setup.sh')], env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / 'frontend/node_modules/.collimate-npm-ci-stamp').is_file()
    assert not (root / 'desktop/node_modules').exists()
    lines = Path(env['TOOLS_LOG']).read_text().splitlines()
    assert lines == ['install_toolchain ', 'install_toolchain --check', 'uv sync --all-packages --all-groups', 'npm ci']
    result = subprocess.run(['bash', str(root / 'setup.sh')], env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    # A rerun with nothing changed reuses the frontend install; uv sync is its own check.
    assert Path(env['TOOLS_LOG']).read_text().splitlines()[4:] == ['install_toolchain ', 'install_toolchain --check', 'uv sync --all-packages --all-groups']


def test_setup_stops_when_its_shell_path_misses_the_toolchain(checkout):
    root, _, _, env = checkout
    shutil.copy2(ROOT / 'setup.sh', root)
    (root / 'build/install_toolchain.sh').write_text('[ "$1" != --check ]\n')
    result = subprocess.run(['bash', str(root / 'setup.sh')], env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 1
    assert "this shell's PATH doesn't find the toolchain" in result.stderr
    assert not Path(env['TOOLS_LOG']).exists()


def toolchain_bins(tmp_path, node_version):
    bins = tmp_path / 'toolchain-bin'
    bins.mkdir()
    tools = {
        'git': 'echo "git version 2.0"',
        'uv': 'echo "uv 0.12.0"',
        'python3': 'echo 3.13.1',
        'node': f'echo v{node_version}',
        'npm': 'echo 11.0.0',
    }
    for name, body in tools.items():
        (bins / name).write_text(f'#!/bin/sh\n{body}\n')
        (bins / name).chmod(0o755)
    return bins


@pytest.mark.parametrize(('node_version', 'code'), [('24.15.0', 0), ('24.14.9', 1), ('25.1.0', 1), ('20.19.4', 1)])
def test_toolchain_check_judges_the_callers_path_and_changes_nothing(tmp_path, node_version, code):
    home = tmp_path / 'home'
    home.mkdir()
    bins = toolchain_bins(tmp_path, node_version)
    env = {'HOME': str(home), 'PATH': f'{bins}:/usr/bin:/bin'}
    result = subprocess.run(['bash', str(ROOT / 'build/install_toolchain.sh'), '--check'], env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == code, result.stdout + result.stderr
    assert ('MISSING node' in result.stderr) == bool(code)
    assert list(home.iterdir()) == []


def test_toolchain_check_names_a_shadowed_local_install(tmp_path):
    home = tmp_path / 'home'
    local = home / '.local/bin'
    local.mkdir(parents=True)
    (local / 'node').write_text('#!/bin/sh\necho v24.15.0\n')
    (local / 'node').chmod(0o755)
    bins = toolchain_bins(tmp_path, '20.19.4')
    env = {'HOME': str(home), 'PATH': f'{bins}:/usr/bin:/bin'}
    result = subprocess.run(['bash', str(ROOT / 'build/install_toolchain.sh'), '--check'], env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 1
    assert f'{local}/node is installed, but PATH resolves {bins}/node' in result.stderr


def test_missing_sdk_fails_before_any_build_step__F1077(checkout):
    root, _, _, env = checkout
    result = subprocess.run(['bash', str(root / 'build/build_server.sh')], env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 1
    assert 'SDK checkout missing' in result.stderr
    assert not Path(env['TOOLS_LOG']).exists()


def test_retired_noop_flag_is_rejected_before_build__F1122(checkout):
    _, home, settings, env = checkout
    before = settings.read_bytes()
    result = run(checkout, '--with-server-check')
    assert result.returncode == 2
    assert 'unknown' in result.stderr.lower()
    assert (home / '.local/share/collimate/app/resources/app.asar').read_text() == 'old app'
    assert settings.read_bytes() == before
    assert not Path(env['TOOLS_LOG']).exists()


def test_help_is_concise_and_lists_supported_flags__F1082(checkout):
    result = run(checkout, '--help')
    assert result.returncode == 0
    assert len(result.stdout.splitlines()) < 35
    for flag in ('--skip', '--wipe', '--no-install', '--install-dir', '--uninstall', '--reset', '--apparmor', '--no-sudo', '--linux', '--mac', '--win', '--dir'):
        assert flag in result.stdout
    assert '--wipe-' not in result.stdout
    assert 'project_desktop_dev_install.md' in result.stdout
    assert '--with-server-check' not in result.stdout
    assert 'SKIP_NPM_CI' not in result.stdout


def test_reset_missing_package_keeps_install_and_settings__F1058(checkout):
    _, home, settings, env = checkout
    npx = Path(env['PATH'].split(':')[0]) / 'npx'
    npx.write_text('#!/bin/bash\nmkdir -p release\n')
    before = settings.read_bytes()
    result = run(checkout, '--reset', '--skip', '--no-sudo')
    assert result.returncode == 1
    assert 'no unpacked app' in result.stderr
    assert (home / '.local/share/collimate/app/resources/app.asar').read_text() == 'old app'
    assert settings.read_bytes() == before


@pytest.mark.parametrize('artifact', ['failure', 'missing', 'empty', 'no-executable', 'no-asar', 'valid'])
def test_darwin_reset_validates_package_before_removing_old_app__F1058(checkout, artifact):
    _, home, _, env = checkout
    bins = Path(env['PATH'].split(':')[0])
    (bins / 'uname').write_text('#!/bin/sh\necho Darwin\n')
    (bins / 'uname').chmod(0o755)
    old = home / 'Applications/Collimate.app/Contents/Resources/app.asar'
    old.parent.mkdir(parents=True)
    old.write_text('old mac app')
    settings = home / 'Library/Application Support/Collimate/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({'serverSpec': {'old': True}, 'keep': 7}))
    before = settings.read_bytes()
    npx = bins / 'npx'
    npx.write_text('''#!/bin/bash
set -e
test -f "$HOME/Applications/Collimate.app/Contents/Resources/app.asar"
echo old-mac-install-present-at-package >> "$TOOLS_LOG"
[ "$ARTIFACT" != failure ] || exit 23
mkdir -p release
[ "$ARTIFACT" != missing ] || exit 0
mkdir -p release/mac/Collimate.app
[ "$ARTIFACT" != empty ] || exit 0
mkdir -p release/mac/Collimate.app/Contents/{MacOS,Resources}
[ "$ARTIFACT" = no-asar ] || echo new-mac > release/mac/Collimate.app/Contents/Resources/app.asar
[ "$ARTIFACT" != no-executable ] || exit 0
printf '#!/bin/sh\\nexit 0\\n' > release/mac/Collimate.app/Contents/MacOS/Collimate
chmod +x release/mac/Collimate.app/Contents/MacOS/Collimate
''')
    result = run(checkout, '--reset', '--skip', '--no-sudo', ARTIFACT=artifact)
    assert 'old-mac-install-present-at-package' in Path(env['TOOLS_LOG']).read_text()
    if artifact == 'valid':
        assert result.returncode == 0, result.stdout + result.stderr
        assert old.read_text().strip() == 'new-mac'
        data = json.loads(settings.read_text())
        assert data['keep'] == 7
        assert data['serverSpec'].startswith('local:')
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert old.read_text() == 'old mac app'
        assert settings.read_bytes() == before


def launcher_icon(entry):
    return Path(next(line[5:] for line in entry.read_text().splitlines() if line.startswith('Icon=')))


def hashed_name(png, suffix='.png'):
    return f'collimate-{hashlib.sha256(png.read_bytes()).hexdigest()[:12]}{suffix}'


def test_installed_launcher_names_its_icon_by_content_and_drops_the_old_mark(checkout):
    root, home, _, env = checkout
    png = root / 'desktop/icons/app-icon.png'
    app = home / '.local/share/collimate/app'
    entry = home / '.local/share/applications/collimate.desktop'
    result = run(checkout, '--skip', '--no-sudo')
    assert result.returncode == 0, result.stdout + result.stderr
    first = launcher_icon(entry)
    assert first == app / hashed_name(png)
    assert first.read_bytes() == png.read_bytes()
    assert (app / hashed_name(png, '.svg')).read_bytes() == (root / 'desktop/icons/app-icon.svg').read_bytes()
    assert 'kbuildsycoca6 --noincremental' in Path(env['TOOLS_LOG']).read_text()
    png.write_text('the new mark')
    result = run(checkout, '--skip', '--no-sudo')
    assert result.returncode == 0, result.stdout + result.stderr
    second = launcher_icon(entry)
    assert second == app / hashed_name(png) != first
    assert sorted(p.name for p in app.glob('collimate-*.*')) == [hashed_name(png), hashed_name(png, '.svg')]
    result = run(checkout, '--uninstall', '--no-sudo')
    assert result.returncode == 0, result.stdout + result.stderr
    assert not second.exists() and not entry.exists()


def test_dev_launcher_copies_its_icon_out_of_the_checkout_by_content(checkout):
    root, home, _, env = checkout
    png = root / 'desktop/icons/app-icon.png'
    entry = home / '.local/share/applications/collimate-dev.desktop'
    icons = home / '.local/share/collimate-dev'
    def launch():
        result = subprocess.run(['bash', str(root / 'build/dev_desktop.sh'), '--skip'], env=env, text=True, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        return result
    launch()
    first = launcher_icon(entry)
    assert first == icons / hashed_name(png)
    assert first.read_bytes() == png.read_bytes()
    assert 'wrote' not in launch().stdout
    png.write_text('the new mark')
    assert 'wrote' in launch().stdout
    assert launcher_icon(entry) == icons / hashed_name(png)
    assert [p.name for p in icons.iterdir()] == [hashed_name(png)]
    assert Path(env['TOOLS_LOG']).read_text().count('kbuildsycoca6 --noincremental') == 2
