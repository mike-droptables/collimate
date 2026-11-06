#!/usr/bin/env python3
"""Guard release versions, verify the wheel, and keep the repository's Latest
release a complete app release.

One version names a release: pyproject.toml's (the one distribution,
``collimate``; the desktop package is stamped with it by
build/desktop_packaging.py), and ``guard`` holds the release tag to
v<version>.

``check-wheel`` reads one built wheel: it is ``collimate`` and nothing else
(the server ``collimate_server`` with its UI, the three bundled trees, the
SDK's home ``collimate-run-sdk/`` holding the SDK packages ``collimate`` and
``gen`` and nothing else, the ``.pth`` naming that home, and the dist-info;
no other top-level entry), every module the snapshot index names is present
with its sha256, and the console scripts start the server and the linter.

``latest`` is the reactive half of the Latest invariant (.github/workflows/
release-guard.yml): electron-updater installs whatever GitHub labels Latest,
so Latest must be a vX.Y.Z release carrying the update feed,
and every other release a prerelease with a tag no semver parser accepts and
no feed file.

``merge-mac-feed`` joins the two mac legs' ``latest-mac.yml`` parts (each
desktop leg builds its own arch on a runner of that arch, so each feed names
its own files only) into the one feed: one version, every file of both, and
the first part's ``path``/``sha512``/``releaseDate``. electron-updater
chooses by arch from the ``files`` list (an arm64 Mac the arm64 files, an
Intel Mac the others), so the first part is the x64 one: a client that reads
only ``path`` gets the build that runs on both.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

APP_TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
# What the app release carries for its one reader, electron-updater: the
# three update feeds.
LATEST_ASSETS = ('latest.yml', 'latest-mac.yml', 'latest-linux.yml')
# A tag semver.valid accepts, after an optional v or = (electron-updater
# reads a slash-separated tag's last segment).
SEMVER = re.compile(r'[v=]?[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?')
_SPACE = re.compile(r'\s*')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk' / 'src'))
from collimate.shared import WHEEL_SDK_HOME  # noqa: E402

# The wheel's top level: the server, the bundled trees, the SDK's home and the
# .pth that names it (build/hatch_build.py); the SDK's home holds its two
# packages and nothing else, since a generator run takes it as its PYTHONPATH.
PACKAGES = ('collimate_server', 'collimate_content', 'collimate_builtins', 'collimate_module_snapshot')
SDK_PACKAGES = ('collimate', 'gen')
SDK_PTH = f'{WHEEL_SDK_HOME}.pth'
REQUIRED = (f'{WHEEL_SDK_HOME}/collimate/__init__.py', f'{WHEEL_SDK_HOME}/collimate/shared/',
            f'{WHEEL_SDK_HOME}/collimate/native_verifier/', f'{WHEEL_SDK_HOME}/gen/__init__.py',
            'collimate_server/__init__.py', 'collimate_server/__main__.py', 'collimate_server/main.py',
            'collimate_server/builtins.json', 'collimate_server/routes/',
            'collimate_server/process_manager/runner_image/Dockerfile',
            'collimate_server/process_manager/base_image/Dockerfile', 'collimate_server/static/index.html',
            'collimate_content/generators/', 'collimate_content/renderers/', 'collimate_content/suggested/',
            'collimate_builtins/', 'collimate_module_snapshot/index.json')
SCRIPTS = {'collimate': 'collimate_server.main:cli_main', 'collimate-lint': 'collimate.lint.cli:main'}


def version(root: Path) -> str:
    value = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    # The version is a version, never a requirement or shell fragment.
    if not isinstance(value, str) or not re.fullmatch(
            r'[0-9]+(?:\.[0-9]+)+(?:a[0-9]+|b[0-9]+|rc[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?', value):
        raise ValueError('pyproject.toml [project] version must be one release version')
    return value


def guard(root: Path) -> None:
    app = version(root)
    # release.yml runs on a pushed tag (the runner's GITHUB_REF_NAME) or on a
    # dispatch, whose rehearsal names its tag as an input (DISPATCH_TAG).
    event = os.environ.get('EVENT_NAME', '')
    if event == 'push':
        tag = os.environ.get('GITHUB_REF_NAME', '')
    elif event == 'workflow_dispatch':
        tag = os.environ.get('DISPATCH_TAG', '')
    else:
        raise ValueError(f'unsupported release event {event!r}')
    if tag != f'v{app}':
        raise ValueError(f'release tag {tag!r} differs from the pyproject.toml version {app}')
    print(f'version {app}; tag {tag}; event {event}')


def check_wheel(root: Path, path: Path) -> None:
    expected = version(root)
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        if len(names) != len(set(names)):
            raise ValueError('wheel contains duplicate entries')
        info = f'collimate-{expected}.dist-info/'
        metadata_paths = [name for name in names if name.endswith('.dist-info/METADATA')]
        if metadata_paths != [info + 'METADATA']:
            raise ValueError(f'wheel must hold exactly {info}METADATA; found {metadata_paths!r}')
        metadata = BytesParser().parsebytes(wheel.read(info + 'METADATA'))
        if metadata['Name'] != 'collimate' or metadata['Version'] != expected:
            raise ValueError(f"wheel is {metadata['Name']} {metadata['Version']}, not collimate {expected}")
        tops = {name.split('/', 1)[0] for name in names}
        allowed = {*PACKAGES, WHEEL_SDK_HOME, SDK_PTH}
        stray = sorted(tops - allowed - {info.rstrip('/')})
        if stray:
            raise ValueError(f'wheel holds {", ".join(stray)} at its top level; only {", ".join(sorted(allowed))} '
                             'and the dist-info belong there')
        beside = sorted({name.split('/')[1] for name in names if name.startswith(f'{WHEEL_SDK_HOME}/')}
                        - set(SDK_PACKAGES))
        if beside:
            raise ValueError(f'wheel holds {", ".join(beside)} in {WHEEL_SDK_HOME}/; only '
                             f'{", ".join(SDK_PACKAGES)} belong there')
        if SDK_PTH not in names or wheel.read(SDK_PTH).decode().splitlines() != [WHEEL_SDK_HOME]:
            raise ValueError(f'wheel must hold {SDK_PTH} naming {WHEEL_SDK_HOME} and nothing else')
        for prefix in REQUIRED:
            present = any(name.startswith(prefix) for name in names) if prefix.endswith('/') else prefix in names
            if not present:
                raise ValueError(f'wheel missing {prefix}')
        for name in names:
            if '/__pycache__/' in name or name.endswith('.pyc') or '/node_modules/' in name:
                raise ValueError(f'wheel holds a build leftover: {name}')
        scripts = {}
        section = None
        for line in wheel.read(info + 'entry_points.txt').decode().splitlines():
            line = line.strip()
            if line.startswith('['):
                section = line
            elif section == '[console_scripts]' and '=' in line:
                key, _, value = line.partition('=')
                scripts[key.strip()] = value.strip()
        if scripts != SCRIPTS:
            raise ValueError(f'wheel console scripts are {scripts!r}, not {SCRIPTS!r}')
        # The module mirror's bundled snapshot: every entry's served bytes.
        mirror = json.loads(wheel.read('collimate_module_snapshot/index.json'))
        if not isinstance(mirror.get('entries'), dict) or not mirror['entries']:
            raise ValueError('wheel has no module mirror snapshot')
        for url, entry in mirror['entries'].items():
            name = 'collimate_module_snapshot/modules/' + entry['hash']
            if name not in names or hashlib.sha256(wheel.read(name)).hexdigest() != entry['hash']:
                raise ValueError(f'module mirror bytes differ for {url}: {name}')
    print(f'{path.name}: collimate {expected}; the SDK alone in {WHEEL_SDK_HOME}/ with its {SDK_PTH}, '
          'server, UI and bundled trees present, module snapshot bytes verified')


def _assets(release: dict) -> set[str]:
    return {asset['name'] for asset in release.get('assets', [])}


def complete(release: dict) -> bool:
    """A published vX.Y.Z release carrying the feed."""
    return (APP_TAG.fullmatch(release['tag_name']) is not None and not release['draft']
            and not release['prerelease'] and set(LATEST_ASSETS) <= _assets(release))


def latest_verdict(releases: list[dict], latest: dict | None) -> tuple[list[str], str | None]:
    """What breaks the Latest invariant, and the release to mark Latest when
    Latest is not a complete app release: the highest complete one."""
    problems = []
    for release in releases:
        tag = release['tag_name']
        if APP_TAG.fullmatch(tag):
            continue
        if not release['draft'] and not release['prerelease']:
            problems.append(f'{tag} is a full release; a release that is not vX.Y.Z must be a prerelease')
        if SEMVER.fullmatch(tag.rsplit('/', 1)[-1]):
            problems.append(f'{tag} reads as a version; a release that is not vX.Y.Z needs a tag '
                            'semver rejects, such as snapshot-YYYYMMDD')
        feed = sorted(name for name in _assets(release) if name.startswith('latest') and name.endswith('.yml'))
        if feed:
            problems.append(f'{tag} carries {", ".join(feed)}; only an app release carries the update feed')
    ranked = sorted((release for release in releases if complete(release)),
                    key=lambda release: tuple(map(int, release['tag_name'][1:].split('.'))))
    newest = ranked[-1]['tag_name'] if ranked else None
    if latest is not None and complete(latest):
        return problems, None
    if latest is None and newest is None:
        return problems, None  # no app release yet: nothing for an update check to find
    if latest is None:
        problems.append(f'no release is Latest while {newest} is a complete app release')
    else:
        missing = sorted(set(LATEST_ASSETS) - _assets(latest))
        problems.append(f"Latest is {latest['tag_name']}, not a complete vX.Y.Z app release"
                        + (f' (it lacks {", ".join(missing)})' if missing else ''))
        if newest is None:
            problems.append('no complete app release exists to mark Latest')
    return problems, newest


def _json_values(text: str) -> list[dict]:
    """Every JSON value in ``text`` (``gh api --paginate --jq '.[]'`` prints one per line)."""
    decoder, values, at = json.JSONDecoder(), [], 0
    while (at := _SPACE.match(text, at).end()) < len(text):
        value, at = decoder.raw_decode(text, at)
        values.append(value)
    return values


def latest(releases_path: Path, latest_path: Path, remark: bool) -> int:
    releases = _json_values(releases_path.read_text())
    current = _json_values(latest_path.read_text())
    problems, target = latest_verdict(releases, current[0] if current else None)
    for problem in problems:
        print(f'::error::{problem}')
    if target and remark:
        subprocess.run(['gh', 'release', 'edit', target, '--latest'], check=True)
        print(f'::error::marked {target} Latest; until now every installed app was offered the release above')
    if problems:
        return 1
    print(f"Latest is {current[0]['tag_name'] if current else 'unset (no app release yet)'}; "
          f'{len(releases)} releases checked')
    return 0


def merge_mac_feed(parts: list[str]) -> str:
    """The one latest-mac.yml of ``parts`` (each a leg's latest-mac.yml text):
    refused unless they name one version and no file twice."""
    import yaml
    feeds = [yaml.safe_load(part) for part in parts]
    if len(feeds) < 2 or not all(isinstance(feed, dict) and isinstance(feed.get('files'), list) for feed in feeds):
        raise ValueError('merge-mac-feed takes two or more latest-mac.yml parts, each with files')
    versions = {feed.get('version') for feed in feeds}
    if len(versions) != 1:
        raise ValueError(f'the mac feed parts name different versions: {sorted(map(str, versions))}')
    files = [entry for feed in feeds for entry in feed['files']]
    urls = [entry.get('url') for entry in files]
    if len(set(urls)) != len(urls):
        raise ValueError(f'the mac feed parts name a file twice: {urls}')
    return yaml.safe_dump({**feeds[0], 'files': files}, sort_keys=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('guard', 'check-wheel', 'latest', 'merge-mac-feed'))
    parser.add_argument('parts', nargs='*', type=Path, help='merge-mac-feed: the legs\' latest-mac.yml, x64 first')
    parser.add_argument('--out', type=Path, help='merge-mac-feed: where the merged latest-mac.yml goes')
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--wheel', type=Path)
    parser.add_argument('--releases', type=Path, help='latest: the repository\'s releases, JSON values')
    parser.add_argument('--latest', type=Path, help='latest: /releases/latest, empty when there is none')
    parser.add_argument('--remark', action='store_true', help='latest: mark the newest complete app release Latest')
    args = parser.parse_args()
    try:
        if args.command == 'guard':
            guard(args.root)
        elif args.command == 'latest':
            if args.releases is None or args.latest is None:
                parser.error('latest requires --releases and --latest')
            return latest(args.releases, args.latest, args.remark)
        elif args.command == 'merge-mac-feed':
            if args.out is None:
                parser.error('merge-mac-feed requires --out')
            args.out.write_text(merge_mac_feed([part.read_text() for part in args.parts]))
            print(f'{args.out}: {len(args.parts)} mac feed parts merged')
        else:
            if args.wheel is None:
                parser.error('check-wheel requires --wheel')
            check_wheel(args.root, args.wheel)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
