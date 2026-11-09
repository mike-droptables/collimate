#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the SDK's git-ignored outputs when they are missing or stale.

The renderer runtime (``shared/renderer-runtime/{react.js,session.js,
common.json,snapshot-codecs.json}``) and the verifier prelude
(``shared/verify/prelude.js``) are built from their sources and committed
nowhere; the manifest committed beside each (``index.json``,
``prelude.json``) pins the sources and the bytes. An output that matches its
manifest is left alone, so a tree that carries them (an unpacked sdist)
needs no Node. Otherwise its builder runs with ``--outputs``, which writes
the outputs and never a manifest; a source edited since its manifest is
refused, and ``python3 scripts/generate.py`` (from the workspace root)
regenerates the manifest.

Run by ``bash build/prepare_sdk_checkout.sh`` after its ``npm ci`` and by
``sdk/hatch_build.py`` for every build. Stdlib only.

    python3 scripts/build_outputs.py    # from sdk/
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREPARE = "bash build/prepare_sdk_checkout.sh"
# The git-ignored outputs (sdk/.gitignore), which the sdist force-includes.
BUILT = ("shared/renderer-runtime/react.js", "shared/renderer-runtime/session.js",
         "shared/renderer-runtime/common.json", "shared/renderer-runtime/snapshot-codecs.json",
         "shared/verify/prelude.js")
# label, builder, verifier (collimate.renderer_runtime), generate.py step
BUILDS = (("renderer runtime", "scripts/build_renderer_runtime.mjs", "verify_runtime_resources", "renderer-runtime"),
          ("verifier prelude", "scripts/build_verify_prelude.mjs", "verify_prelude_resources", "verify-prelude"))


class BuildRefused(Exception):
    """An output cannot be built here, with the reason and the remedy."""


def ensure(root: Path = ROOT) -> list[str]:
    """Build what is missing or stale under ``root``, then verify every
    output against its manifest; answer the labels built."""
    sys.path.insert(0, str(root / "src"))
    try:
        from collimate import renderer_runtime
        built = []
        for label, builder, verifier, step in BUILDS:
            verify = getattr(renderer_runtime, verifier)
            try:
                verify(root)
                continue
            except renderer_runtime.SourceChanged as error:
                raise BuildRefused(f"{error}: its committed manifest is stale; run python3 scripts/generate.py "
                                   f"--only {step} from the workspace root") from None
            except (OSError, ValueError):
                pass  # missing or stale: build it
            node = shutil.which("node")
            if node is None:
                raise BuildRefused(f"the {label} is not built and node is not on PATH: install Node "
                                   f"(shared/contracts/toolchain.json), then run {PREPARE}")
            result = subprocess.run([node, str(root / builder), "--outputs"], cwd=root, capture_output=True, text=True)
            if result.returncode:
                raise BuildRefused(f"node {builder} --outputs failed (exit {result.returncode}); its npm "
                                   f"packages are installed by {PREPARE}:\n{result.stdout}{result.stderr}")
            try:
                verify(root)
            except ValueError as error:
                raise BuildRefused(f"the {label} built here differs from its committed manifest ({error}): "
                                   f"run python3 scripts/generate.py --only {step} and compare the "
                                   "manifest's diff") from None
            built.append(label)
        return built
    finally:
        sys.path.remove(str(root / "src"))


def main() -> int:
    try:
        built = ensure()
    except (BuildRefused, OSError, ValueError) as error:
        print(f"build_outputs: {error}", file=sys.stderr)
        return 1
    print(f"build_outputs: built the {' and the '.join(built)}" if built
          else "build_outputs: the renderer runtime and the verifier prelude are current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
