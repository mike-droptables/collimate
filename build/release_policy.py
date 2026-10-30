#!/usr/bin/env python3
"""Guard release versions, prepare the exact SDK pin, verify the app wheel, and
keep the repository's Latest release a complete app release.

One version names a release: sdk/pyproject.toml (collimate-sdk),
api/pyproject.toml (collimate), desktop/package.json and the release tag
v<version>. The app wheel requires exactly collimate-sdk==<sdk version>.

``latest`` is the reactive half of the Latest invariant (.github/workflows/
release-guard.yml): electron-updater installs whatever GitHub labels Latest,
so Latest must be a vX.Y.Z release carrying the update feed and content.zip,
and every other release a prerelease with a tag no semver parser accepts and
no feed file.
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
# What the app release carries for its readers: the three update feeds
# (electron-updater) and the content tree (api/suggested.py GITHUB_CONTENT_ZIP).
LATEST_ASSETS = ('latest.yml', 'latest-mac.yml', 'latest-linux.yml', 'content.zip')
# A tag semver.valid accepts, after an optional v or = (electron-updater
# reads a slash-separated tag's last segment).
SEMVER = re.compile(r'[v=]?[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?')
_SPACE = re.compile(r'\s*')


def pinned_sdk(root: Path) -> str:
    version = tomllib.loads((root / 'sdk/pyproject.toml').read_text())['project']['version']
    # The pin is a version, never a requirement or shell fragment.
    if not isinstance(version, str) or not re.fullmatch(
            r'[0-9]+(?:\.[0-9]+)+(?:a[0-9]+|b[0-9]+|rc[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?', version):
        raise ValueError('sdk/pyproject.toml [project] version must be one release version')
    return version


def guard(root: Path) -> None:
    api = tomllib.loads((root / 'api/pyproject.toml').read_text())['project']['version']
    desktop = json.loads((root / 'desktop/package.json').read_text())['version']
    if api != desktop:
        raise ValueError(f'api/pyproject.toml version {api} differs from desktop/package.json version {desktop}')
    sdk = pinned_sdk(root)
    if sdk != api:
        raise ValueError(f'sdk/pyproject.toml version {sdk} differs from api/pyproject.toml and desktop/package.json version {api}')
    # release.yml runs on a pushed tag (the runner's GITHUB_REF_NAME) or on a
    # dispatch, whose rehearsal names its tag as an input (DISPATCH_TAG).
    event = os.environ.get('EVENT_NAME', '')
    if event == 'push':
        tag = os.environ.get('GITHUB_REF_NAME', '')
    elif event == 'workflow_dispatch':
        tag = os.environ.get('DISPATCH_TAG', '')
    else:
        raise ValueError(f'unsupported release event {event!r}')
    if tag != f'v{api}':
        raise ValueError(f'release tag {tag!r} differs from the sdk/pyproject.toml, api/pyproject.toml and desktop/package.json version {api}')
    print(f'sdk, app and desktop version {api}; tag {tag}; event {event}')


def pin_sdk(root: Path) -> None:
    version = pinned_sdk(root)
    path = root / 'api/pyproject.toml'
    source = path.read_text()
    dependencies = tomllib.loads(source)['project']['dependencies']
    selected = [req for req in dependencies if re.match(r'(?i)^collimate[-_]sdk(?=[\s<=>!~;\[]|$)', req)]
    if len(selected) != 1:
        raise ValueError('expected exactly one collimate-sdk project dependency')
    # Retain the checkout's comments and every unrelated field. Only a complete
    # quoted dependency line is eligible; refuse unfamiliar layout before write.
    pattern = re.compile(r'(?m)^(\s*[\'"])' + re.escape(selected[0]) + r'([\'"]\s*,?\s*(?:#.*)?)$')
    prepared, count = pattern.subn(lambda m: m[1] + f'collimate-sdk=={version}' + m[2], source)
    if count != 1:
        raise ValueError('expected exactly one standalone collimate-sdk dependency line')
    # Parse before replacing, so malformed preparation cannot destroy the input.
    tomllib.loads(prepared)
    path.write_text(prepared)
    print(f'prepared collimate-sdk=={version}')


def check_wheel(root: Path, path: Path) -> None:
    expected = f'collimate-sdk=={pinned_sdk(root)}'
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        if len(names) != len(set(names)):
            raise ValueError('wheel contains duplicate entries')
        metadata_paths = [name for name in names if name.endswith('.dist-info/METADATA')]
        if len(metadata_paths) != 1:
            raise ValueError('wheel must contain exactly one distribution metadata file')
        metadata = BytesParser().parsebytes(wheel.read(metadata_paths[0]))
        requirements = [req for req in metadata.get_all('Requires-Dist', [])
                        if re.match(r'(?i)^collimate[-_]sdk(?=[\s<=>!~;\[]|$)', req)]
        if len(requirements) != 1 or requirements[0].replace(' ', '').lower() != expected:
            raise ValueError(f'wheel must require exactly {expected}; found {requirements!r}')
        for prefix in ('static/', 'main.py', 'builtins.json', 'routes/', 'process_manager/runner_image/Dockerfile',
                       'collimate_content/generators/', 'collimate_content/renderers/',
                       'collimate_content/suggested/', 'collimate_builtins/', 'collimate_module_snapshot/index.json'):
            present = any(name.startswith(prefix) for name in names) if prefix.endswith('/') else prefix in names
            if not present:
                raise ValueError(f'wheel missing {prefix}')
        # The bundled trees ship once, under their package names (api/hatch_build.py).
        for prefix in ('templates/', 'generators/', 'renderers/', 'builtin/', 'collimate/',
                       'content/', 'builtins/', 'module-snapshot/'):
            if any(name.startswith(prefix) for name in names):
                raise ValueError(f'wheel holds {prefix}; the bundled trees ship as collimate_content/, '
                                 'collimate_builtins/ and collimate_module_snapshot/')
        # The module mirror's bundled snapshot: every entry's served bytes.
        mirror = json.loads(wheel.read('collimate_module_snapshot/index.json'))
        if not isinstance(mirror.get('entries'), dict) or not mirror['entries']:
            raise ValueError('wheel has no module mirror snapshot')
        for url, entry in mirror['entries'].items():
            name = 'collimate_module_snapshot/modules/' + entry['hash']
            if name not in names or hashlib.sha256(wheel.read(name)).hexdigest() != entry['hash']:
                raise ValueError(f'module mirror bytes differ for {url}: {name}')
    print(f'{path.name}: {expected}; bundled trees present, module snapshot bytes verified')


def _assets(release: dict) -> set[str]:
    return {asset['name'] for asset in release.get('assets', [])}


def complete(release: dict) -> bool:
    """A published vX.Y.Z release carrying the feed and content.zip."""
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('guard', 'pin-sdk', 'check-wheel', 'latest'))
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--wheel', type=Path)
    parser.add_argument('--releases', type=Path, help='latest: the repository\'s releases, JSON values')
    parser.add_argument('--latest', type=Path, help='latest: /releases/latest, empty when there is none')
    parser.add_argument('--remark', action='store_true', help='latest: mark the newest complete app release Latest')
    args = parser.parse_args()
    try:
        if args.command == 'guard':
            guard(args.root)
        elif args.command == 'pin-sdk':
            pin_sdk(args.root)
        elif args.command == 'latest':
            if args.releases is None or args.latest is None:
                parser.error('latest requires --releases and --latest')
            return latest(args.releases, args.latest, args.remark)
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
