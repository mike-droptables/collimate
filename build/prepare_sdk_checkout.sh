#!/usr/bin/env bash
# Install the tooling a clean checkout builds and checks with: the npm
# packages in sdk/scripts and sdk/packages/renderer-hooks, the SDK's built
# renderer runtime and verifier prelude, its native compiler and verifier
# payloads (fetched at their pinned hashes, verified), the workspace venv,
# then the module snapshot's bodies. Then check the committed generated files
# with the repository's one command:
#
#     bash build/prepare_sdk_checkout.sh
#     python3 scripts/generate.py --check
#
# This script writes no committed file, so the check after it reads the tree
# as committed. What it builds and fetches is git-ignored, like node_modules:
# the runtime and the prelude are built against their committed manifests
# (sdk/scripts/build_outputs.py never writes a manifest, and refuses a source
# edited since its manifest), and the bodies come from the orphan branch
# module-snapshot, verified against the committed module-snapshot/index.json
# (build/snapshot_store.py fetch, which fetches the branch from origin when
# no local ref holds them). It does not run the check itself: CI's
# setup-toolchain action runs this script in every job, while the check runs
# once per workflow, as the first step of sdk-ci.yml's test job and test.yml's
# api-tests job; and a tree in the middle of an edit should still get its
# tooling (only a runtime or prelude source edited since its manifest stops
# it, naming the generate.py step that rewrites the manifest). After an edit,
# `python3 scripts/generate.py` regenerates every generated file and checks.
set -euo pipefail
app_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sdk_root="$app_root/sdk"
cd "$sdk_root"
npm ci --prefix scripts
npm ci --prefix packages/renderer-hooks
python3 scripts/build_outputs.py
python3 scripts/prepare_renderer_compilers.py
python3 scripts/prepare_verifier_engine.py
cd "$app_root"
uv sync --all-packages --all-groups
python3 build/snapshot_store.py fetch
