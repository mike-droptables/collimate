#!/usr/bin/env python3
"""Check every platform wheel of the one distribution, collimate, and the complete
native payload set before publication: each wheel carries exactly its target's
verifier payloads and is tagged for it, and every wheel holds the same bytes
but those (build/release_policy.py check-wheel checks the rest of one wheel)."""
from __future__ import annotations

import argparse
import email
from pathlib import Path
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from collimate._host.verifier_engine import contract, verify_payload, wheel_payload_targets  # noqa: E402
from build_outputs import BUILT  # noqa: E402
from collimate.shared import WHEEL_SDK_HOME  # noqa: E402

# Where the wheel puts the SDK packages, collimate and gen.
SDK = WHEEL_SDK_HOME + '/'

# The git-ignored runtime and prelude the build makes, and the manifests that pin them.
BUILT_SHARED = (*(SDK + 'collimate/' + name for name in BUILT),
                SDK + 'collimate/shared/renderer-runtime/index.json', SDK + 'collimate/shared/verify/prelude.json')


def check_wheel(path):
    locked = contract()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate wheel paths')
        metadata_names = [name for name in names if name.endswith('.dist-info/METADATA')]
        wheel_names = [name for name in names if name.endswith('.dist-info/WHEEL')]
        if len(metadata_names) != 1 or len(wheel_names) != 1:
            raise ValueError('Wheel has ambiguous distribution metadata')
        metadata = email.message_from_bytes(archive.read(metadata_names[0]))
        wheel = email.message_from_bytes(archive.read(wheel_names[0]))
        if metadata['Name'] != 'collimate' or wheel['Root-Is-Purelib'] != 'false':
            raise ValueError('A native collimate wheel must use its platform installation scheme')
        if any('quickjs' in value.lower() for value in metadata.get_all('Requires-Dist', [])):
            raise ValueError('The wheel still requires an independently compiled engine')
        tags = set(wheel.get_all('Tag', []))
        hosts = [target for target, entry in locked['targets'].items()
                 if tags == {f'cp312-abi3-{tag}' for tag in entry['platform_tag'].split('.')}]
        if len(hosts) != 1:
            raise ValueError('Native wheel tags do not identify one supported host target')
        target = hosts[0]
        entry = locked['targets'][target]
        if not path.name.endswith(f"-cp312-abi3-{entry['platform_tag']}.whl"):
            raise ValueError('Native wheel filename does not match its host target')
        required = wheel_payload_targets(target)
        payload_names = ('manifest.json', '_quickjs.abi3.so', 'LICENSE.quickjs-ng', 'LICENSE.quickjs-engine')
        expected = {SDK + 'collimate/native_verifier/' + payload_target + '/' + name
                    for payload_target in required for name in payload_names}
        actual = {name for name in names if name.startswith(SDK + 'collimate/native_verifier/') and not name.endswith('/')}
        if actual != expected:
            raise ValueError('Wheel must carry exactly its host and both Linux container payloads')
        with tempfile.TemporaryDirectory(prefix='native-wheel-check-') as scratch:
            for payload_target in required:
                payload = Path(scratch) / payload_target
                payload.mkdir()
                for name in payload_names:
                    (payload / name).write_bytes(archive.read(SDK + 'collimate/native_verifier/' + payload_target + '/' + name))
                verify_payload(payload, payload_target)
        for prefix in (*(SDK + name for name in (
                           'collimate/__init__.py', 'collimate/_host/packaging/', 'collimate/shared/',
                           'collimate/renderer_compilers/manifest.json', 'collimate/renderer_compilers/linux-x64/esbuild',
                           'collimate/renderer_bundle_contract.json', 'collimate/renderer_tools/renderer_bundle.mjs',
                           'gen/__init__.py')), *BUILT_SHARED):
            if not (any(name.startswith(prefix) for name in names) if prefix.endswith('/') else prefix in names):
                raise ValueError(f'Wheel missing {prefix}')
        # The first-party content trees ship as their own packages, never inside the SDK.
        for prefix in (SDK + 'collimate/generators/', SDK + 'collimate/renderers/', SDK + 'collimate/builtin/',
                       SDK + 'collimate/suggested/'):
            if any(name.startswith(prefix) for name in names):
                raise ValueError(f'The SDK package carries the content {prefix}')
        return target, metadata['Version']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--all', action='store_true', help='Require exactly the complete four-target wheel set')
    args = parser.parse_args()
    wheels = sorted(args.directory.glob('collimate-*.whl'))
    if not wheels:
        raise ValueError('No native collimate wheels')
    records = [(path, *check_wheel(path)) for path in wheels]
    baseline = None
    for path, _, _ in records:
        with zipfile.ZipFile(path) as archive:
            shared = {name: archive.read(name) for name in archive.namelist()
                      if not name.endswith('/') and not name.startswith(SDK + 'collimate/native_verifier/')
                      and not name.endswith(('.dist-info/WHEEL', '.dist-info/RECORD'))}
        if baseline is None:
            baseline = shared
        elif shared != baseline:
            raise ValueError('Native wheels differ in metadata or shared resource bytes')
    if len({target for _, target, _ in records}) != len(records) or len({version for _, _, version in records}) != 1:
        raise ValueError('Native wheel set contains duplicate targets or different versions')
    if args.all and {target for _, target, _ in records} != set(contract()['targets']):
        raise ValueError('Native release requires all four supported targets')
    for path, target, version in records:
        print(f'{target}: {path.name} ({version}) verified')


if __name__ == '__main__':
    main()
