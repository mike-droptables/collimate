#!/bin/bash
# =============================================================================
# setup.sh — One-time (or after dependency changes) environment setup
#
# Run this:
#   - The first time you clone the repo
#   - After pulling changes that modify a pyproject.toml, uv.lock or frontend/package.json/package-lock.json
#   - After switching Node/Python versions
#
# What it installs:
#   1. The toolchain — git, uv, Python, Node/npm — at the versions the build
#      needs, via build/install_toolchain.sh (`--check` there only reports)
#   2. The SDK's git-ignored build inputs, the workspace venv (sdk/ and api/)
#      via uv, and the module snapshot's bodies, via
#      build/prepare_sdk_checkout.sh (what CI runs in every job)
#   3. npm packages for the Vite frontend (frontend/)
#
# A rerun redoes only what changed: the toolchain step installs only what is
# missing or too old, the SDK preparation reuses its cached downloads and the
# snapshot bodies already there, `uv sync` is a no-op when the venv matches
# uv.lock, and npm ci reruns only when package.json/package-lock.json is newer
# than the last install or Node changed (build/npm_ci_if_stale.sh).
#
# =============================================================================
set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=================================="
echo "  Collimate — Dev Environment     "
echo "=================================="

# ---------------------------------------------------------------------------
# 1. Toolchain
# ---------------------------------------------------------------------------
echo ""
echo "[1/3] Checking the toolchain..."

bash "$ROOT_DIR/build/install_toolchain.sh"
# The install verifies with ~/.local/bin first; the steps below, and the build
# scripts after them, run with this shell's PATH, so judge that one.
bash "$ROOT_DIR/build/install_toolchain.sh" --check >/dev/null || {
    echo "error: this shell's PATH doesn't find the toolchain above; fix PATH as noted, open a new shell, rerun ./setup.sh" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# 2. SDK preparation and the Python workspace
# ---------------------------------------------------------------------------
echo ""
echo "[2/3] Preparing the SDK and the Python workspace..."

# uv sync alone fails on a fresh clone: the SDK's build hook verifies payloads
# (esbuild, the native verifier) that only this script fetches.
bash "$ROOT_DIR/build/prepare_sdk_checkout.sh"

# ---------------------------------------------------------------------------
# 3. Frontend (Vite)
# ---------------------------------------------------------------------------
echo ""
echo "[3/3] Installing frontend dependencies..."

bash "$ROOT_DIR/build/npm_ci_if_stale.sh" "$ROOT_DIR/frontend"

echo ""
echo "=================================="
echo "  Setup complete."
echo ""
echo "  Run the app (build/ has the one-command flows):"
echo "    build/dev_server.sh            — build + run the localhost server"
echo "    build/dev_desktop.sh           — build + run the Electron app"
echo "    build/build_server.sh          — build only: FastAPI + UI bundle"
echo "    build/build_desktop.sh         — package this checkout as a desktop app"
echo "=================================="
