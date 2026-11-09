#!/usr/bin/env bash
# Build the browser demo: stage → patch → vendor → assemble → dist/.
#
# One entry point, no flags. Everything it does is in build/ (python, stdlib
# only); this wrapper only resolves the two source trees and hands over.
#
# Environment:
#   COLLIMATE_SDK_DIR     the SDK source tree. Default: the in-repo sdk/.
#   PLAYGROUND_DEMO_DIR   the demo content dir (channel.yaml, channel/,
#                         generators/). Default: playground/demo.
#   PLAYGROUND_FRESH_NPM  set to 1 to force `npm ci` in the staged frontend
#                         even when APP/frontend/node_modules can be reused.
set -euo pipefail

for tool in npm python3; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "build.sh: '$tool' not found on PATH" >&2
        exit 1
    }
done

PLAYGROUND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$PLAYGROUND_DIR/.." && pwd)"

export COLLIMATE_SDK_DIR="${COLLIMATE_SDK_DIR:-$APP_DIR/sdk}"
export PLAYGROUND_DEMO_DIR="${PLAYGROUND_DEMO_DIR:-$PLAYGROUND_DIR/demo}"

# `python3 <dir>` runs build/__main__.py with build/ as sys.path[0], so the
# step modules import as plain top-level modules and never collide with the
# PyPI `build` package.
exec python3 -u "$PLAYGROUND_DIR/build" "$APP_DIR" "$PLAYGROUND_DIR"
