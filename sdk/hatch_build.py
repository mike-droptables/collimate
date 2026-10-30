"""Fail builds with incomplete native resources; build never downloads tools.

The renderer runtime and the verifier prelude are git-ignored build outputs:
scripts/build_outputs.py builds them with Node when missing or stale and
verifies them against their committed manifests, so rebuilding an sdist,
which carries them, needs no Node."""
import os
import subprocess
import sys
from pathlib import Path
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        subprocess.run([sys.executable, str(root / 'scripts/sync_distribution_metadata.py'), '--check'], check=True)
        subprocess.run([sys.executable, str(root / 'scripts/prepare_renderer_compilers.py'), '--verify-only'], check=True)
        sys.path.insert(0, str(root / 'scripts'))
        sys.path.insert(0, str(root / 'src'))
        try:
            import build_outputs
            try:
                build_outputs.ensure(root)
            except build_outputs.BuildRefused as error:
                raise RuntimeError(f'collimate-sdk build: {error}') from None
            if self.target_name == 'sdist':
                # Git-ignored, so the sdist carries them only by force.
                for name in build_outputs.BUILT:
                    build_data['force_include'][str(root / name)] = name
            from collimate._host.verifier_engine import contract, native_target, verify_payload, wheel_payload_targets
            locked = contract()
            prepared = root / 'src/collimate/native_verifier'
            if self.target_name == 'sdist':
                for target in locked['targets']:
                    verify_payload(prepared / target, target)
            else:
                target = os.environ.get('COLLIMATE_WHEEL_TARGET') or native_target()
                for payload_target in wheel_payload_targets(target):
                    verify_payload(prepared / payload_target, payload_target)
                    build_data['force_include'][str(prepared / payload_target)] = 'collimate/native_verifier/' + payload_target
                build_data['pure_python'] = False
                build_data['tag'] = 'cp312-abi3-' + locked['targets'][target]['platform_tag']
        finally:
            sys.path.remove(str(root / 'src'))
            sys.path.remove(str(root / 'scripts'))
