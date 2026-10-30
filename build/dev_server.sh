#!/bin/bash
# =============================================================================
# dev_server.sh — Build this checkout, then run the server out of it
#
# The one-command loop for working on the app in a browser. It runs
# build/build_server.sh (the SDK renderer runtime, Vite → api/static/)
# and then starts the FastAPI server from api/ with `uv run`, so the page you
# open is the tree you just built. Ctrl-C stops it.
#
# Usage:
#   bash build/dev_server.sh [--skip] [--wipe] [--port N] [--data-dir PATH]
#
#   --skip           start against the api/static/ already on disk. The Vite
#                    build dominates the launch, so skip it while iterating on
#                    Python alone — but a frontend change then won't be in the
#                    bundle being served.
#   --wipe           delete the data dir's .collimate/ (channels, databases,
#                    blobs) before starting — a fresh server. The go-to after
#                    a change that old channel data can't survive (seeded
#                    renderers/generators are channel data and only refresh
#                    on re-seed).
#   --port N         forwarded to the server CLI (default 5001; loopback only)
#   --data-dir PATH  forwarded to the server CLI (else $COLLIMATE_DATA, else CWD)
#
# Anything else is an error — the flags above are the whole surface.
#
# For frontend hot-reload instead, leave a server running here and use
# `cd frontend && npm run dev`; Vite proxies /api to 127.0.0.1:5001. The
# server this script starts runs with COLLIMATE_DEV=1, the only way a server
# trusts the Vite dev origins (localhost/127.0.0.1 :5173/:5174).
# =============================================================================
set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$ROOT_DIR/api"

SKIP=0
WIPE=0
DATA_DIR_ARG=""
SERVER_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --skip) SKIP=1 ;;
        --wipe) WIPE=1 ;;
        -h|--help)
            # The header block: line 2 through the last comment line before
            # `set -e`. Computed rather than a fixed range, so editing the
            # header can't silently truncate --help — the same rule as
            # build/dev_desktop.sh and build/build_desktop.sh.
            sed -n '2,/^set -e/p' "${BASH_SOURCE[0]}" | sed '$d'
            exit 0
            ;;
        # The server CLI's own flags pass through; both spellings, since
        # argparse accepts `--port 5050` and `--port=5050` alike. The
        # data dir is also remembered here so --wipe targets the same
        # directory the server is about to use.
        --data-dir)
            [ $# -ge 2 ] || {
                echo "error: $1 needs a value" >&2
                exit 2
            }
            DATA_DIR_ARG="$2"
            SERVER_ARGS+=("$1" "$2")
            shift
            ;;
        --data-dir=*)
            DATA_DIR_ARG="${1#--data-dir=}"
            SERVER_ARGS+=("$1")
            ;;
        -p|--port)
            [ $# -ge 2 ] || {
                echo "error: $1 needs a value" >&2
                exit 2
            }
            SERVER_ARGS+=("$1" "$2")
            shift
            ;;
        -p=*|--port=*) SERVER_ARGS+=("$1") ;;
        *)
            echo "error: unknown argument: $1" >&2
            echo "       Usage: bash build/dev_server.sh [--skip] [--wipe] [--port N] [--data-dir PATH]" >&2
            exit 2
            ;;
    esac
    shift
done

if [ "$WIPE" = "1" ]; then
    # Mirror the server CLI's resolution exactly (main.py cli_main):
    # --data-dir beats $COLLIMATE_DATA beats the CWD — and the CWD the
    # server will get is $API_DIR, because that's where this script execs
    # it from. The server absolutises a relative path against that CWD
    # (os.path.abspath), so a relative one is anchored at $API_DIR here too,
    # not at the directory this script was run from.
    DATA_DIR="${DATA_DIR_ARG:-${COLLIMATE_DATA:-$API_DIR}}"
    DATA_DIR="${DATA_DIR/#\~/$HOME}"
    case "$DATA_DIR" in /*) ;; *) DATA_DIR="$API_DIR/$DATA_DIR" ;; esac
    target="$DATA_DIR/.collimate"
    if [ -d "$target" ]; then
        echo "[dev_server] wiping $target"
        rm -rf "$target"
    fi
fi

# Both paths start the server with `uv run`.
command -v uv >/dev/null 2>&1 || {
    echo "error: 'uv' not found on PATH — see README.md prerequisites" >&2
    exit 1
}

if [ "$SKIP" = "1" ]; then
    echo "[dev_server] --skip — serving api/static/ as it stands"
    [ -d "$API_DIR/static" ] || {
        echo "error: --skip but $API_DIR/static is missing." >&2
        echo "       Run 'bash build/dev_server.sh' once without --skip." >&2
        exit 1
    }
else
    bash "$ROOT_DIR/build/build_server.sh"
fi

echo ""
echo "[dev_server] Starting the server from $API_DIR..."
cd "$API_DIR"
# The dev UI on :5173/:5174 calls this server directly (getApiUrl in
# frontend/src/lib/api.ts); only a server started with this flag trusts it.
export COLLIMATE_DEV=1
# exec, so Ctrl-C and any supervisor's signals reach the server directly
# instead of a wrapper shell that would leave it orphaned.
exec uv run python main.py "${SERVER_ARGS[@]}"
