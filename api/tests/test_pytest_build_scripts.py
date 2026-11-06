"""Execute developer build scripts with isolated homes and fake build-tool seams."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
HEAD = '0123456789abcdef0123456789abcdef01234567'
WHEEL = 'collimate-0.2.0-cp312-abi3-manylinux_2_28_x86_64.whl'
FEEDS = 'pyproject.toml uv.lock api sdk content builtins module-snapshot frontend desktop build'


def _packaging():
    spec = importlib.util.spec_from_file_location('desktop_packaging', ROOT / 'build/desktop_packaging.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


packaging = _packaging()


def targets(unpacked):
    """The electron-builder target arguments a build on this host passes."""
    host = packaging.host_platform(sys.platform), packaging.host_arch(platform.machine())
    return ' '.join(packaging.host_targets(*host, unpacked))


FAKE_BUILD = '''
def build(wheel, out, target, uv, *, release=False, env=None):
    """The test seam: no download and no uv, the same manifest."""
    match = WHEEL.fullmatch(wheel.name)
    if match is None or not wheel.is_file():
        raise Refused(f"{wheel} is not a collimate wheel")
    shutil.rmtree(out, ignore_errors=True)
    (out / "bin").mkdir(parents=True)
    (out / "lib").mkdir()
    (out / "bin/python3").write_text("#!/bin/sh\\nexit 0\\n")
    (out / "bin/python3").chmod(0o755)
    (out / "lib/x.txt").write_text("fixture")
    manifest = {
        "schema": SCHEMA,
        "target": host_target(),
        "python": {"version": PYTHON_VERSION, "standalone": STANDALONE_RELEASE},
        "collimate": {"version": match["version"], "wheel": wheel.name, "sha256": sha256(wheel)},
        "hash": tree_hash(out),
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\\n", encoding="utf-8")
    with open(os.environ["TOOLS_LOG"], "a") as log:
        log.write(f"server_env {wheel} -> {out}{' (release)' if release else ''}\\n")
    return manifest


'''


def _fake_server_env(root):
    """build/server_env.py with its build() replaced: it downloads CPython and installs with uv."""
    text = (ROOT / 'build/server_env.py').read_text()
    marker = 'if __name__ == "__main__":'
    assert text.count(marker) == 1
    head, tail = text.split(marker)
    (root / 'build/server_env.py').write_text(head + FAKE_BUILD + marker + tail)


@pytest.fixture
def checkout(tmp_path):
    # A space in the checkout's path: nothing may split it.
    root = tmp_path / 'my app'
    shutil.copytree(ROOT / 'build', root / 'build')
    for name in ('desktop', 'frontend', 'api/collimate_server/static', 'sdk/scripts'):
        (root / name).mkdir(parents=True)
    (root / 'sdk/scripts/build_renderer_runtime.mjs').write_text('// isolated runtime builder seam')
    # The one version: build_desktop.sh prints it from the checkout's pyproject.toml.
    (root / 'pyproject.toml').write_text('[project]\nname = "collimate"\nversion = "0.2.0"\n')
    for name in ('desktop', 'frontend'):
        (root / name / 'package.json').write_text(json.dumps({'name': 'collimate-desktop'}))
        (root / name / 'package-lock.json').write_text('{}')
    icons = root / 'desktop/icons'
    (icons / 'hicolor/16x16/apps').mkdir(parents=True)
    for name in ('app-icon.png', 'app-icon.svg', 'hicolor/16x16/apps/collimate.png'):
        (icons / name).write_text('fixture icon')
    (root / 'pyproject.toml').write_text('[project]\nname = "collimate"\nversion = "0.2.0"\n')
    _fake_server_env(root)
    home = tmp_path / 'home'
    home.mkdir()
    settings = home / '.config/Collimate/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({'keep': 7}))
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
        if [ "$2" = --prefix ]; then mkdir -p "$3/node_modules"; else mkdir -p node_modules; fi
    elif [ "$1" = run ]; then
        [ "${FAIL_BUILD:-0}" = 0 ] || exit 22
        mkdir -p dist
        [ "${NO_INDEX:-0}" = 1 ] || echo '<!doctype html>' > dist/index.html
    fi ;;
npx)
    [ "${FAIL_PACKAGE:-0}" = 0 ] || exit 23
    if [ "$2" = electron-builder ]; then
        mkdir -p release/linux-unpacked/resources
        echo new > release/linux-unpacked/resources/app.asar
        printf '#!/bin/sh\\nexit 0\\n' > release/linux-unpacked/collimate-desktop
        chmod +x release/linux-unpacked/collimate-desktop
        case " $* " in
            *" dir:"*) ;;
            *) echo "appimage $*" > release/Collimate-0.2.0-linux-x86_64.AppImage
               echo deb > release/collimate-desktop_0.2.0_amd64.deb ;;
        esac
    fi ;;
uv)
    if [ "$1" = build ]; then
        while [ $# -gt 0 ]; do
            case "$1" in
                -o) out="$2"; shift ;;
            esac
            shift
        done
        if [ "${SKIP_WHEEL:-0}" = 0 ]; then
            mkdir -p "$out"
            echo "collimate wheel bytes" > "$out/%(wheel)s"
        fi
    fi ;;
git)
    case " $* " in
        *" rev-parse HEAD "*) echo %(head)s ;;
        *" status "*) printf '%%s' "${GIT_STATUS:-}" ;;
    esac ;;
esac
'''
    fake %= {'wheel': WHEEL, 'head': HEAD}
    for name in ('npm', 'npx', 'uv', 'git', 'kbuildsycoca6'):
        path = bins / name
        path.write_text(fake)
        path.chmod(0o755)
    node = shutil.which('node')
    assert node, 'Node is required by the developer build scripts'
    node_bin = Path(node).parent
    env = {k: v for k, v in os.environ.items() if not k.startswith(('SNAP', 'FLATPAK', 'XDG_'))}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'), XDG_DATA_HOME=str(home / '.local/share'), PATH=f'{bins}:{node_bin}:/usr/bin:/bin', TOOLS_LOG=str(tmp_path / 'tools.log'), TMPDIR=str(tmp_path))
    return root, home, settings, env


def run(checkout, *args, cwd=None, **flags):
    root, _, _, env = checkout
    return subprocess.run(['bash', str(root / 'build/build_desktop.sh'), *args], env=env | flags, cwd=cwd or root, text=True, capture_output=True, timeout=20)


@pytest.mark.parametrize('script', ['build_desktop.sh', 'dev_desktop.sh', 'build_server.sh'])
def test_build_flows_refresh_stale_lock_and_reuse_current_install__F1087_F1122_F1084(checkout, script):
    root, _, _, env = checkout
    project = root / ('frontend' if script == 'build_server.sh' else 'desktop')
    args = {'build_server.sh': [], 'dev_desktop.sh': ['--skip'], 'build_desktop.sh': ['--dir']}[script]
    def execute():
        result = subprocess.run(['bash', str(root / 'build' / script), *args], env=env, cwd=root, text=True, capture_output=True, timeout=20)
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
    # build_desktop.sh's first run also installs the frontend through build_server.sh.
    assert lines.count('npm ci') == (3 if script == 'build_desktop.sh' else 2)
    assert not any(line.startswith('npm install') for line in lines)
    assert (project / 'package-lock.json').read_bytes() == lock


def test_setup_installs_only_the_frontend_lock_and_reuses_a_current_install__F1059(checkout):
    root, _, _, env = checkout
    shutil.copy2(ROOT / 'setup.sh', root)
    (root / 'build/install_toolchain.sh').write_text('echo "install_toolchain $*" >> "$TOOLS_LOG"\n')
    (root / 'build/prepare_sdk_checkout.sh').write_text('echo "prepare_sdk_checkout $*" >> "$TOOLS_LOG"\n')
    result = subprocess.run(['bash', str(root / 'setup.sh')], env=env, cwd=root, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / 'frontend/node_modules/.collimate-npm-ci-stamp').is_file()
    assert not (root / 'desktop/node_modules').exists()
    lines = Path(env['TOOLS_LOG']).read_text().splitlines()
    assert lines == ['install_toolchain ', 'install_toolchain --check', 'prepare_sdk_checkout ', 'npm ci']
    result = subprocess.run(['bash', str(root / 'setup.sh')], env=env, cwd=root, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    # A rerun with nothing changed reuses the frontend install; the SDK preparation is its own check.
    assert Path(env['TOOLS_LOG']).read_text().splitlines()[4:] == ['install_toolchain ', 'install_toolchain --check', 'prepare_sdk_checkout ']


def test_setup_stops_when_its_shell_path_misses_the_toolchain(checkout):
    root, _, _, env = checkout
    shutil.copy2(ROOT / 'setup.sh', root)
    (root / 'build/install_toolchain.sh').write_text('[ "$1" != --check ]\n')
    result = subprocess.run(['bash', str(root / 'setup.sh')], env=env, cwd=root, text=True, capture_output=True, timeout=20)
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
    shutil.rmtree(root / 'sdk')
    result = subprocess.run(['bash', str(root / 'build/build_server.sh')], env=env, cwd=root, text=True, capture_output=True, timeout=20)
    assert result.returncode == 1
    assert 'SDK sources missing' in result.stderr
    assert not Path(env['TOOLS_LOG']).exists()


@pytest.mark.parametrize('flag', ['--with-server-check', '--no-install', '--install-dir=/x', '--uninstall', '--reset', '--apparmor', '--no-sudo', '--skip', '--wipe',
                                  '--linux', '--mac', '--win'])
def test_unknown_and_retired_flags_are_rejected_before_build(checkout, flag):
    root, _, settings, env = checkout
    before = settings.read_bytes()
    result = run(checkout, flag)
    assert result.returncode == 2
    assert 'unknown' in result.stderr.lower()
    assert settings.read_bytes() == before
    assert not Path(env['TOOLS_LOG']).exists()
    assert not (root / 'desktop/release').exists()


def local_config(root):
    return json.loads((root / 'dist/desktop/electron-builder.json').read_text())


def server_hash(root):
    return json.loads((root / 'dist/desktop/server/collimate-server.json').read_text())['hash']


def test_a_build_packages_the_checkout_in_order_and_touches_no_settings(checkout):
    root, home, settings, env = checkout
    before = settings.read_bytes()
    result = run(checkout, '--dir')
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / 'desktop/release/linux-unpacked/resources/app.asar').read_text().strip() == 'new'
    assert settings.read_bytes() == before
    assert sorted(p.name for p in home.iterdir()) == ['.config']
    assert 'No installers (--dir build)' in result.stdout
    assert 'AppImage copied' not in result.stdout
    assert f'commit:   {HEAD[:10]}\n' in result.stdout
    assert 'server:   a frozen environment of the collimate wheel built from this checkout' in result.stdout
    assert 'feed:     none\n' in result.stdout
    wheels = root / 'dist/desktop/wheels'
    config = root / 'dist/desktop/electron-builder.json'
    server = root / 'dist/desktop/server'
    # The stamp is read before anything builds; the UI is in api/collimate_server/static before
    # the app wheel takes it; the shell is packaged with the generated config.
    assert Path(env['TOOLS_LOG']).read_text().splitlines() == [
        f'git -C {root} rev-parse HEAD',
        f'git -C {root} status --porcelain --untracked-files=normal -- {FEEDS}',
        f'npm ci --prefix {root}/sdk/scripts',
        f'npm ci --prefix {root}/sdk/packages/renderer-hooks',
        'npm ci',
        'npm run build',
        f'uv build --wheel -o {wheels}',
        f'server_env {wheels}/{WHEEL} -> {server}',
        'npm ci',
        'npm run build',
        f'npx --no-install electron-builder --config {config} {targets(True)} --publish never',
    ]
    # status.showUntrackedFiles=no must not hide a new module the wheels pack.
    assert '--untracked-files=normal' in Path(env['TOOLS_LOG']).read_text()
    assert f'targets:  {targets(True)}\n' in result.stdout
    assert (root / 'api/collimate_server/static/index.html').is_file()
    stamp = local_config(root)['extraMetadata']['collimate']
    assert stamp['signed'] is False
    build = stamp['build']
    assert build['commit'] == HEAD
    assert build['dirty'] is False
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', build['builtAt'])
    assert build['server'] == server_hash(root)
    manifest = json.loads((server / 'collimate-server.json').read_text())
    assert manifest['collimate'] == {
        'version': '0.2.0', 'wheel': WHEEL, 'sha256': hashlib.sha256((wheels / WHEEL).read_bytes()).hexdigest()}
    assert local_config(root)['extraResources'] == [{'from': str(server), 'to': 'server'}]
    assert local_config(root)['publish'] is None


def test_uncommitted_changes_where_the_package_comes_from_stamp_the_build_dirty(checkout):
    root, _, _, _ = checkout
    result = run(checkout, '--dir', GIT_STATUS='?? sdk/new_module.py\n')
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'commit:   {HEAD[:10]} (uncommitted changes)' in result.stdout
    assert local_config(root)['extraMetadata']['collimate']['build']['dirty'] is True


def test_a_build_starts_from_an_empty_staging_folder(checkout):
    root, _, _, _ = checkout
    stale = root / 'dist/desktop/wheels/collimate-0.0.9-py3-none-any.whl'
    stale.parent.mkdir(parents=True)
    stale.write_text('an earlier build')
    result = run(checkout, '--dir')
    assert result.returncode == 0, result.stdout + result.stderr
    assert not stale.exists()
    assert local_config(root)['extraMetadata']['collimate']['build']['server'] == server_hash(root)


def test_a_build_without_the_ui_stops_before_the_wheels(checkout):
    root, _, _, env = checkout
    result = run(checkout, '--dir', NO_INDEX='1')
    assert result.returncode == 1
    assert 'api/collimate_server/static/index.html is missing' in result.stderr
    assert not any(line.startswith('uv ') for line in Path(env['TOOLS_LOG']).read_text().splitlines())
    assert not (root / 'desktop/release').exists()


def test_a_build_that_yields_no_wheel_stops_before_packaging(checkout):
    root, _, _, env = checkout
    result = run(checkout, '--dir', SKIP_WHEEL='1')
    assert result.returncode == 1
    assert 'expected one collimate wheel in' in result.stderr
    assert not any(line.startswith('npx ') for line in Path(env['TOOLS_LOG']).read_text().splitlines())
    assert not any(line.startswith('server_env ') for line in Path(env['TOOLS_LOG']).read_text().splitlines())
    assert not (root / 'dist/desktop/electron-builder.json').exists()
    assert not (root / 'desktop/release').exists()


def test_a_given_wheel_replaces_the_build_of_one_and_is_carried_as_it_is(checkout, tmp_path):
    root, _, _, env = checkout
    given = tmp_path / 'invoked here/given'
    given.mkdir(parents=True)
    (given / WHEEL).write_text('a wheel from the dist job')
    for args in (['--wheel', f'given/{WHEEL}'], [f'--wheel=given/{WHEEL}']):
        result = run(checkout, '--dir', *args, cwd=given.parent)
        assert result.returncode == 0, result.stdout + result.stderr
        assert f'server:   a frozen environment of {WHEEL}\n' in result.stdout
        lines = Path(env['TOOLS_LOG']).read_text().splitlines()
        assert not any(line.startswith('uv ') or 'sdk/scripts' in line for line in lines)
        assert f'server_env {given / WHEEL} -> {root}/dist/desktop/server' in lines
        manifest = json.loads((root / 'dist/desktop/server/collimate-server.json').read_text())
        assert manifest['collimate']['sha256'] == hashlib.sha256(b'a wheel from the dist job').hexdigest()
        assert local_config(root)['extraMetadata']['collimate']['build']['server'] == manifest['hash']
        assert not (root / 'dist/desktop/wheels').exists()
        Path(env['TOOLS_LOG']).unlink()


def test_a_wheel_that_is_not_a_file_is_refused_before_anything_builds(checkout):
    root, _, _, env = checkout
    result = run(checkout, '--dir', '--wheel', 'nope.whl')
    assert result.returncode == 2
    assert 'is not a file' in result.stderr
    assert run(checkout, '--wheel').returncode == 2
    assert not Path(env['TOOLS_LOG']).exists()
    assert not (root / 'desktop/release').exists()


def test_a_release_build_keeps_the_update_feed(checkout):
    root, _, _, env = checkout
    result = run(checkout, '--dir', '--release')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'feed:     the release feed (GitHub)\n' in result.stdout
    # On a Mac the environment is signed as the release signs the app (build/mac_codesign.py).
    assert f'server_env {root}/dist/desktop/wheels/{WHEEL} -> {root}/dist/desktop/server (release)' in (
        Path(env['TOOLS_LOG']).read_text().splitlines())
    assert 'publish' not in local_config(root)
    assert local_config(root)['extraMetadata']['collimate']['signed'] is False


def test_arguments_after_the_separator_follow_the_build_flags(checkout):
    root, _, _, env = checkout
    result = run(checkout, '--dir', '--', '--x64')
    assert result.returncode == 0, result.stdout + result.stderr
    config = root / 'dist/desktop/electron-builder.json'
    assert Path(env['TOOLS_LOG']).read_text().splitlines()[-1] == (
        f'npx --no-install electron-builder --config {config} {targets(True)} --publish never --x64')
    assert run(checkout, '--').returncode == 2


def test_a_linux_build_copies_the_appimage_to_the_home_folder_under_its_standard_name(checkout):
    root, home, _, _ = checkout
    stale = home / 'CollimateApp' / 'Collimate-0.2.0-linux-x86_64.AppImage'
    stale.parent.mkdir()
    stale.write_text('an earlier build')
    stale.chmod(0o644)
    result = run(checkout)
    assert result.returncode == 0, result.stdout + result.stderr
    config = root / 'dist/desktop/electron-builder.json'
    assert stale.read_text() == f'appimage --no-install electron-builder --config {config} {targets(False)} --publish never\n'
    assert os.access(stale, os.X_OK)
    assert sorted(p.name for p in stale.parent.iterdir()) == ['Collimate-0.2.0-linux-x86_64.AppImage']
    assert f'AppImage copied to {stale}' in result.stdout
    assert (root / 'desktop/release/collimate-desktop_0.2.0_amd64.deb').is_file()
    # Each installer is listed by size and file name, whatever the checkout's path holds.
    listed = result.stdout.split('Installers in desktop/release/:\n', 1)[1].splitlines()[:2]
    assert sorted(re.fullmatch(r' {4}\S+ +(.+)', line).group(1) for line in listed) == [
        'Collimate-0.2.0-linux-x86_64.AppImage', 'collimate-desktop_0.2.0_amd64.deb']


@pytest.mark.parametrize('spelling', ['two-args', 'equals'])
def test_appimage_dir_flag_chooses_the_folder(checkout, tmp_path, spelling):
    root, home, _, _ = checkout
    target = tmp_path / 'elsewhere' / 'nested'
    args = ['--appimage-dir', str(target)] if spelling == 'two-args' else [f'--appimage-dir={target}']
    result = run(checkout, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    config = root / 'dist/desktop/electron-builder.json'
    assert (target / 'Collimate-0.2.0-linux-x86_64.AppImage').read_text() == (
        f'appimage --no-install electron-builder --config {config} {targets(False)} --publish never\n')
    assert not (home / 'CollimateApp').exists()
    assert run(checkout, '--appimage-dir').returncode == 2


@pytest.mark.parametrize('spelling', ['two-args', 'equals'])
def test_a_relative_appimage_dir_is_the_invocation_directorys(checkout, tmp_path, spelling):
    root, _, _, _ = checkout
    here = tmp_path / 'invoked here'
    here.mkdir()
    args = ['--appimage-dir', 'out/apps'] if spelling == 'two-args' else ['--appimage-dir=out/apps']
    result = run(checkout, *args, cwd=here)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (here / 'out/apps/Collimate-0.2.0-linux-x86_64.AppImage').is_file()
    assert not (root / 'desktop/out').exists()


def test_an_empty_appimage_dir_is_refused_before_anything_builds(checkout):
    root, _, _, env = checkout
    result = run(checkout, '--appimage-dir=')
    assert result.returncode == 2
    assert '--appimage-dir needs a value' in result.stderr
    assert not Path(env['TOOLS_LOG']).exists()
    assert not (root / 'desktop/release').exists()


def test_help_is_concise_and_lists_supported_flags__F1082(checkout):
    result = run(checkout, '--help')
    assert result.returncode == 0
    assert len(result.stdout.splitlines()) < 35
    for flag in ('--appimage-dir', '--dir', '--release', '--wheel'):
        assert flag in result.stdout
    for retired in ('--skip', '--wipe', '--install', '--uninstall', '--reset', '--apparmor', '--sudo',
                    '--linux', '--mac', '--win'):
        assert retired not in result.stdout
    assert 'project_desktop_dev_install.md' in result.stdout
    assert '--with-server-check' not in result.stdout
    assert 'SKIP_NPM_CI' not in result.stdout


def launcher_icon(entry):
    return Path(next(line[5:] for line in entry.read_text().splitlines() if line.startswith('Icon=')))


def hashed_name(png, suffix='.png'):
    return f'collimate-{hashlib.sha256(png.read_bytes()).hexdigest()[:12]}{suffix}'


def test_dev_launcher_copies_its_icon_out_of_the_checkout_by_content(checkout):
    root, home, _, env = checkout
    png = root / 'desktop/icons/app-icon.png'
    entry = home / '.local/share/applications/collimate-dev.desktop'
    icons = home / '.local/share/collimate-dev'
    def launch():
        result = subprocess.run(['bash', str(root / 'build/dev_desktop.sh'), '--skip'], env=env, cwd=root, text=True, capture_output=True, timeout=20)
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
