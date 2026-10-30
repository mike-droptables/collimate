"""One license: MIT, byte-identical in every member, named in every manifest."""
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_member_license_is_the_root_mit_text():
    root = (ROOT / 'LICENSE').read_bytes()
    assert root.startswith(b'MIT License\n')
    assert (ROOT / 'sdk/LICENSE').read_bytes() == root
    assert (ROOT / 'api/LICENSE').read_bytes() == root


def test_every_manifest_names_mit():
    for member in ('sdk', 'api'):
        project = tomllib.loads((ROOT / member / 'pyproject.toml').read_text())['project']
        assert project['license'] == 'MIT', member
        assert project['license-files'] == ['LICENSE'], member
    for package in ('desktop/package.json', 'sdk/scripts/package.json', 'sdk/packages/renderer-hooks/package.json'):
        assert json.loads((ROOT / package).read_text())['license'] == 'MIT', package


def test_no_other_license_identifier_survives():
    import subprocess
    tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True, text=True, check=True).stdout.split('\0')
    offenders = []
    for name in tracked:
        if not name or name.startswith(('docs/', 'llm/')) or 'module-snapshot' in name or 'LICENSES' in name or 'CREDITS' in name:
            continue
        path = ROOT / name
        if not path.is_file() or path == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        if 'SPDX-License-Identifier:' in text and 'SPDX-License-Identifier: MIT' not in text:
            offenders.append(name)
        if 'FSL-1.1' in text:
            offenders.append(name)
    assert offenders == []
