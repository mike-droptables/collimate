"""The build hook of the one distribution, ``collimate``.

It adds what the wheel carries beside its three Python packages (``collimate``
and ``gen`` from sdk/src, ``collimate_server`` from api/), and builds or
verifies each part first; it never downloads or compiles anything:

- the SDK's home: pyproject.toml's ``sources`` put ``collimate`` and ``gen``
  under ``collimate-run-sdk/`` (``collimate.shared.WHEEL_SDK_HOME``, which
  this hook checks the mapping against), and the hook adds
  ``collimate-run-sdk.pth``, which puts that directory on ``sys.path``. It
  holds the SDK's two packages alone, so a generator run takes it as its
  PYTHONPATH and none of the server's dependencies with it;
- the SDK's canonical runtime metadata is fresh
  (``sdk/scripts/sync_distribution_metadata.py --check``);
- the SDK's package data: ``sdk/shared`` as ``collimate/shared``, the
  prepared renderer compilers (``sdk/scripts/prepare_renderer_compilers.py
  --verify-only``) as ``collimate/renderer_compilers`` and the renderer
  bundler's Node tools as ``collimate/renderer_tools`` (all under the SDK's
  home in the wheel);
- the SDK's renderer runtime and verifier prelude, git-ignored build outputs,
  built when missing or stale and checked against their committed manifests
  (``sdk/scripts/build_outputs.py``, which needs Node only to rebuild; an
  sdist carries the outputs, so rebuilding from it needs none);
- the native verifier payload: a wheel carries its target's (and both Linux
  payloads, for containers) and is tagged for that target
  (``COLLIMATE_WHEEL_TARGET``, else this host); an sdist carries all four;
- ``content/``, ``builtins/`` and ``module-snapshot/`` (every body its
  ``index.json`` names, which ``python3 build/snapshot_store.py fetch``
  writes) as ``collimate_content/``, ``collimate_builtins/`` and
  ``collimate_module_snapshot/`` in the wheel, at their own paths in the
  sdist;
- the built UI, ``api/collimate_server/static/``, when ``build/build_server.sh``
  or a release job has built it; ``build/release_policy.py check-wheel``
  refuses a release wheel without it.

An editable install returns at once and copies nothing: a checkout reads its
trees, payloads and UI in place, and its ``sdk/src`` (on ``sys.path`` by
pyproject.toml's ``dev-mode-dirs``) holds the two SDK packages alone too.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# checkout directory -> package inside the wheel
TREES = {
    "content": "collimate_content",
    "builtins": "collimate_builtins",
    "module-snapshot": "collimate_module_snapshot",
}
# The renderer bundler's Node side, which the installed SDK runs (collimate._host.renderer_bundle).
RENDERER_TOOLS = ("renderer_bundle.mjs", "renderer_bundle_core.mjs", "renderer_bundle_core.d.mts",
                  "package.json", "package-lock.json", "bundle_renderer_test.mjs")
STATIC = "api/collimate_server/static"
FETCH = "python3 build/snapshot_store.py fetch"


def check_snapshot(snapshot: Path) -> None:
    """Every body ``snapshot/index.json`` names is under ``modules/`` with its sha256."""
    entries = json.loads((snapshot / "index.json").read_bytes())["entries"]
    for digest in sorted({entry["hash"] for entry in entries.values()}):
        body = snapshot / "modules" / digest
        if not body.is_file() or hashlib.sha256(body.read_bytes()).hexdigest() != digest:
            raise FileNotFoundError(f"module-snapshot/modules/{digest} is missing or differs from its "
                                    f"sha256 ({body}); run {FETCH}")


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return  # a dev install reads the checkout, never a copy
        root = Path(self.root)
        sdk = root / "sdk"
        wheel = self.target_name == "wheel"
        force = build_data["force_include"]
        subprocess.run([sys.executable, str(sdk / "scripts/sync_distribution_metadata.py"), "--check"], check=True)
        subprocess.run([sys.executable, str(sdk / "scripts/prepare_renderer_compilers.py"), "--verify-only"],
                       check=True)
        compilers = sdk / "build/renderer_compilers"
        sys.path.insert(0, str(sdk / "scripts"))
        sys.path.insert(0, str(sdk / "src"))
        try:
            from collimate.shared import WHEEL_SDK_HOME as home
            if wheel:
                mapped = self.build_config.sources.get("sdk/src" + os.sep)
                if mapped != home + os.sep:
                    raise RuntimeError(f"collimate build: pyproject.toml maps sdk/src to {mapped!r}; the SDK's "
                                       f"home is {home}/ (collimate.shared.WHEEL_SDK_HOME)")
                self._scratch = tempfile.mkdtemp(prefix="collimate-wheel-")
                pth = Path(self._scratch) / f"{home}.pth"
                pth.write_text(home + "\n", encoding="utf-8")
                force[str(pth)] = pth.name
                force[str(sdk / "shared")] = f"{home}/collimate/shared"
                force[str(compilers)] = f"{home}/collimate/renderer_compilers"
                for name in RENDERER_TOOLS:
                    force[str(sdk / "scripts" / name)] = f"{home}/collimate/renderer_tools/{name}"
            else:
                force[str(compilers)] = "sdk/build/renderer_compilers"
            import build_outputs
            try:
                build_outputs.ensure(sdk)
            except build_outputs.BuildRefused as error:
                raise RuntimeError(f"collimate build: {error}") from None
            if not wheel:
                # Ignored by version control, so the sdist carries them only by force.
                for name in build_outputs.BUILT:
                    force[str(sdk / name)] = f"sdk/{name}"
            from collimate._host.verifier_engine import contract, native_target, verify_payload, wheel_payload_targets
            locked = contract()
            prepared = sdk / "src/collimate/native_verifier"
            if not wheel:
                for target in locked["targets"]:
                    verify_payload(prepared / target, target)
                    force[str(prepared / target)] = f"sdk/src/collimate/native_verifier/{target}"
            else:
                target = os.environ.get("COLLIMATE_WHEEL_TARGET") or native_target()
                for payload_target in wheel_payload_targets(target):
                    verify_payload(prepared / payload_target, payload_target)
                    force[str(prepared / payload_target)] = f"{home}/collimate/native_verifier/{payload_target}"
                build_data["pure_python"] = False
                build_data["tag"] = "cp312-abi3-" + locked["targets"][target]["platform_tag"]
        finally:
            sys.path.remove(str(sdk / "src"))
            sys.path.remove(str(sdk / "scripts"))
        for tree, package in TREES.items():
            source = root / tree
            if not source.is_dir():
                raise FileNotFoundError(f"bundled tree missing: {source}")
            force[str(source)] = package if wheel else tree
        check_snapshot(root / "module-snapshot")
        static = root / STATIC
        if (static / "index.html").is_file():
            force[str(static)] = "collimate_server/static" if wheel else STATIC

    def finalize(self, version, build_data, artifact_path):
        scratch = getattr(self, "_scratch", None)
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
