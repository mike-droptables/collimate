#!/bin/bash
# Reuse a successful lockfile install until either dependency manifest is newer
# or Node changed (native modules are built against the Node that installed them).
# Usage: bash build/npm_ci_if_stale.sh PACKAGE_DIR
set -e
cd "${1:?package directory is required}"
STAMP="node_modules/.collimate-npm-ci-stamp"
NODE_VERSION="$(node --version 2>/dev/null || echo none)"
if [ ! -f package.json ] || [ ! -f package-lock.json ] \
   || [ ! -d node_modules ] || [ ! -f "$STAMP" ] \
   || [ package.json -nt "$STAMP" ] || [ package-lock.json -nt "$STAMP" ] \
   || [ "$(cat "$STAMP")" != "$NODE_VERSION" ]; then
    rm -f "$STAMP"
    npm ci
    # npm ci recreates node_modules; a failed install must never create this stamp.
    echo "$NODE_VERSION" > "$STAMP"
fi
