"""Execute the release manifest against Linux target-native artifact names and
the catalog's content.zip."""
import os
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_release_manifest_accepts_target_native_linux_names_and_refuses_partial_set_P5F06(tmp_path):
    config = yaml.safe_load((ROOT / 'desktop/electron-builder.yml').read_text())
    workflow = yaml.safe_load((ROOT / '.github/workflows/release.yml').read_text())
    check = next(step['run'] for step in workflow['jobs']['assets']['steps']
                 if step.get('name') == 'Check the asset set is complete')
    version = '43.7.0'
    assets = tmp_path / 'assets'
    assets.mkdir()

    def artifact(platform, arch, extension):
        name = config['artifactName']
        for key, value in {'productName': config['productName'], 'version': version,
                           'os': platform, 'arch': arch, 'ext': extension}.items():
            name = name.replace('${' + key + '}', value)
        return name

    # electron-builder's Linux targets substitute their native architecture
    # labels into ${arch}: AppImage uses x86_64; deb uses amd64.
    # The real Linux packaging rehearsal independently confirms these names.
    linux_names = [artifact('linux', 'x86_64', 'AppImage'),
                   artifact('linux', 'amd64', 'deb')]
    names = linux_names + [artifact('mac', arch, extension)
                           for arch in ('arm64', 'x64') for extension in ('dmg', 'zip')]
    names += [artifact('win', 'x64', 'exe'), 'latest.yml', 'latest-mac.yml', 'latest-linux.yml', 'content.zip']
    assert len(names) == 11
    for name in names:
        (assets / name).write_bytes(b'synthetic release artifact')

    def run_check():
        return subprocess.run(['bash', '-c', check], cwd=tmp_path,
                              env={**os.environ, 'VERSION': version},
                              capture_output=True, text=True)

    complete = run_check()
    assert complete.returncode == 0, complete.stdout + complete.stderr
    assert complete.stdout.count('  ok      ') == len(names)
    for name in linux_names + ['content.zip', 'latest-linux.yml']:
        (assets / name).unlink()
        missing = run_check()
        assert missing.returncode == 1, missing.stdout + missing.stderr
        assert f'MISSING {name}' in missing.stdout
        (assets / name).write_bytes(b'synthetic release artifact')
