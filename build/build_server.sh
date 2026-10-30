#!/bin/bash
# =============================================================================
# build_server.sh — Package the FastAPI server with the Vite UI bundled in
#
# Readies api/ to serve this checkout: the server reads the bundled trees
# (content/, builtins/, module-snapshot/) and the SDK's built renderer
# runtime in place, so the build only refreshes that runtime and bundles
# the UI into api/static/.
#
# What this does:
#   1. Rebuilds the SDK's renderer runtime (sdk/shared/renderer-runtime/),
#      which /api/sdk/*.js serves, from its sources.
#   2. Builds the Vite frontend  →  frontend/dist/
#   3. Copies it into api/static/ so FastAPI can serve it from /.
#      (api/main.py already mounts api/static/ when that directory exists.)
#   4. Prints the install and run commands.
#
# Running the result:
#   Install deps (collimate-sdk resolves to the sdk/ workspace member named
#   in api/pyproject.toml [tool.uv.sources]), from the repo root:
#       uv sync --all-packages --all-groups
#   Then run:
#       cd api && uv run python main.py
#   The app is then available at http://localhost:5001 (no separate frontend).
#   The distributable form is the PyPI wheel built by
#   .github/workflows/release.yml.
#
# Usage:
#   bash build/build_server.sh
# =============================================================================
set -e

# Fail fast with a clear message if required tools aren't installed.
for tool in npm node; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "error: '$tool' not found on PATH — see README.md prerequisites" >&2
        exit 1
    }
done


ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$ROOT_DIR/frontend"
API_DIR="$ROOT_DIR/api"
STATIC_DIR="$API_DIR/static"
SDK_DIR="$ROOT_DIR/sdk"

if [ ! -d "$SDK_DIR" ]; then
    echo "error: SDK checkout missing — $SDK_DIR is the workspace member api/pyproject.toml requires." >&2
    echo "       See README.md prerequisites." >&2
    exit 1
fi

echo "=================================="
echo "  Build: FastAPI + Bundled UI     "
echo "=================================="

# ---------------------------------------------------------------------------
# 1. Rebuild the SDK's renderer runtime from its sources
# ---------------------------------------------------------------------------
echo ""
echo "[1/3] Building the SDK renderer runtime..."
echo "     using SDK checkout at $SDK_DIR"
npm ci --prefix "$SDK_DIR/scripts"
npm ci --prefix "$SDK_DIR/packages/renderer-hooks"
node "$SDK_DIR/scripts/build_renderer_runtime.mjs"

# ---------------------------------------------------------------------------
# 2. Build Vite frontend
# ---------------------------------------------------------------------------
echo ""
echo "[2/3] Building Vite frontend..."
cd "$FRONTEND_DIR"
bash "$ROOT_DIR/build/npm_ci_if_stale.sh" "$FRONTEND_DIR"
npm run build          # output: frontend/dist/

# ---------------------------------------------------------------------------
# 3. Copy into api/static/
# ---------------------------------------------------------------------------
echo ""
echo "[3/3] Copying UI into api/static/..."
rm -rf "$STATIC_DIR"
cp -r "$FRONTEND_DIR/dist" "$STATIC_DIR"

echo ""
echo "=================================="
echo "  Build complete."
echo ""
echo "  To install dependencies (from the repo root):"
echo "    uv sync --all-packages --all-groups"
echo ""
echo "  To run the server:"
echo "    cd api && uv run python main.py"
echo "    Open http://localhost:5001"
echo "=================================="
