#!/bin/bash
# build_desktop.sh — Package this checkout as the self-contained desktop app
# for this machine's platform and CPU: the one way every package is made,
# release.yml's desktop legs included.
# Usage: bash build/build_desktop.sh [flags] [-- electron-builder arguments]
#   --dir                the unpacked app only; no installer
#   --release            a published build: the update feed, and signing when
#                        MAC_SIGNING / WIN_SIGNING are true (release.yml);
#                        without it the package has no feed and is unsigned
#   --wheel FILE         carry this collimate wheel (release.yml passes the
#                        dist job's for this target) instead of building the
#                        UI and the wheel from this checkout
#   --appimage-dir DIR   where a built AppImage is copied under its standard
#                        name, replacing one of the same name; relative to
#                        the current directory (default: ~/CollimateApp)
#   -h|--help            print this usage
# Unknown flags are errors; anything else for electron-builder goes after `--`.
# The package carries its server as a frozen environment (build/server_env.py:
# a standalone CPython with the wheel and its locked dependencies, run with no
# network and no download, and the uv its generator runs start with) and a
# build stamp (the commit, whether the tree had uncommitted changes, the build
# time and the environment's hash).
# Windows packages are connect-only and carry no server. Installers land in
# desktop/release/. Running the result, profiles, icons and the dev run:
# docs/ai_tools/notes/2026-10-11/project_desktop_dev_install.md
set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP_DIR="$ROOT_DIR/desktop"
RELEASE_DIR="$DESKTOP_DIR/release"
STAGE_DIR="$ROOT_DIR/dist/desktop"
WHEELS_DIR="$STAGE_DIR/wheels"
SERVER_DIR="$STAGE_DIR/server"
CONFIG="$STAGE_DIR/electron-builder.json"
APPIMAGE_DIR="$HOME/CollimateApp"
PYTHON="${PYTHON:-python3}"
UNPACKED=
RELEASE=
WHEEL=
PASSTHROUGH=()

# A path given on the command line is used after the build has changed
# directory, so it is made absolute against the directory the script was
# invoked from.
absolute() {
    case "$1" in
        /*) printf '%s' "$1" ;;
        *) printf '%s' "$PWD/$1" ;;
    esac
}

needs_value() {
    [ -n "$2" ] || {
        echo "error: $1 needs a value" >&2
        exit 2
    }
}

while [ $# -gt 0 ]; do
    case "$1" in
        --appimage-dir)
            shift
            needs_value --appimage-dir "${1-}"
            APPIMAGE_DIR="$(absolute "$1")"
            ;;
        --appimage-dir=*)
            needs_value --appimage-dir "${1#--appimage-dir=}"
            APPIMAGE_DIR="$(absolute "${1#--appimage-dir=}")"
            ;;
        --wheel)
            shift
            needs_value --wheel "${1-}"
            WHEEL="$(absolute "$1")"
            ;;
        --wheel=*)
            needs_value --wheel "${1#--wheel=}"
            WHEEL="$(absolute "${1#--wheel=}")"
            ;;
        -h|--help)
            # The header block: line 2 through the last comment line before
            # `set -e`. Computed rather than a fixed range so editing the
            # header can't silently truncate --help.
            sed -n '2,/^set -e/p' "${BASH_SOURCE[0]}" | sed '$d'
            exit 0
            ;;
        --dir) UNPACKED=--dir ;;
        --release) RELEASE=--release ;;
        --)
            shift
            [ $# -gt 0 ] || {
                echo "error: nothing follows '--' (electron-builder arguments go after it)" >&2
                exit 2
            }
            PASSTHROUGH=("$@")
            break
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            echo "       Usage: bash build/build_desktop.sh [--dir] [--release] [--wheel FILE] [--appimage-dir DIR] [-- <electron-builder args>]" >&2
            echo "       (bash build/build_desktop.sh --help for the full list)" >&2
            exit 2
            ;;
    esac
    shift
done

# Fail fast with a clear message if required tools aren't installed.
for tool in node npm npx git "$PYTHON"; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "error: '$tool' not found on PATH — see README.md prerequisites" >&2
        exit 1
    }
done
HOST="$("$PYTHON" -c 'import sys; print(sys.platform)')"
case "$HOST" in
    darwin) PLATFORM=mac; SERVER=1 ;;
    linux) PLATFORM=linux; SERVER=1 ;;
    win32) PLATFORM=win; SERVER= ;;
    *) echo "error: no desktop package for platform $HOST" >&2; exit 1 ;;
esac
if [ -n "$SERVER" ]; then
    command -v uv >/dev/null 2>&1 || {
        echo "error: 'uv' not found on PATH — the server environment is built with it" >&2
        exit 1
    }
    if [ -n "$WHEEL" ] && [ ! -f "$WHEEL" ]; then
        echo "error: --wheel $WHEEL is not a file" >&2
        exit 2
    fi
elif [ -n "$WHEEL" ]; then
    echo "error: a Windows package carries no server, so it takes no --wheel" >&2
    exit 2
fi

# The stamp describes the tree as it was handed to the build, so it is read
# before anything runs: build_server.sh regenerates tracked renderer-runtime
# files, and a build must not make its own tree look edited. Only the paths
# that feed the package or shape it (pyproject.toml, uv.lock and build/ hold
# what builds it) count; untracked files there count too, whatever
# status.showUntrackedFiles says, because the wheel takes files from disk,
# not from git.
COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD)" || {
    echo "error: $ROOT_DIR is not a git checkout; a package is stamped with its commit" >&2
    exit 1
}
CHANGES="$(git -C "$ROOT_DIR" status --porcelain --untracked-files=normal -- \
    pyproject.toml uv.lock api sdk content builtins module-snapshot frontend desktop build)"
if [ -n "$CHANGES" ]; then
    TREE_FLAG=--dirty
    TREE_NOTE=" (uncommitted changes)"
else
    TREE_FLAG=--clean
    TREE_NOTE=""
fi
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# This machine's targets, for its CPU only: the server environment and the
# wheel's native binding are the host's, so another arch's artifact could
# not run them. Asked before anything builds, so an unsupported machine
# stops here.
TARGETS="$("$PYTHON" "$ROOT_DIR/build/desktop_packaging.py" targets $UNPACKED)"
read -r -a TARGET_ARGS <<< "$TARGETS"
APP_VERSION="$("$PYTHON" -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$ROOT_DIR/pyproject.toml")"

echo "=================================="
echo "  Build: Collimate desktop app    "
echo "=================================="
echo ""
echo "  version:  $APP_VERSION"
echo "  commit:   ${COMMIT:0:10}$TREE_NOTE"
if [ -z "$SERVER" ]; then
    echo "  server:   none (Windows is connect-only)"
elif [ -n "$WHEEL" ]; then
    echo "  server:   a frozen environment of $(basename "$WHEEL")"
else
    echo "  server:   a frozen environment of the collimate wheel built from this checkout"
fi
echo "  feed:     $([ -n "$RELEASE" ] && echo 'the release feed (GitHub)' || echo 'none')"
echo "  targets:  $TARGETS"

# Nothing from an earlier run may be mistaken for this one's.
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"

# ---------------------------------------------------------------------------
# 1. The server: the UI and the wheel (unless given), then the frozen
#    environment → dist/desktop/server/
# ---------------------------------------------------------------------------
if [ -n "$SERVER" ]; then
    if [ -z "$WHEEL" ]; then
        echo ""
        echo "[1/3] Building the server's UI (build/build_server.sh) and the collimate wheel..."
        bash "$ROOT_DIR/build/build_server.sh"
        # The wheel takes api/collimate_server/static/ as it finds it, so an
        # empty one would package an app with no UI.
        [ -f "$ROOT_DIR/api/collimate_server/static/index.html" ] || {
            echo "error: api/collimate_server/static/index.html is missing after build/build_server.sh" >&2
            exit 1
        }
        cd "$ROOT_DIR"
        uv build --wheel -o "$WHEELS_DIR"
        shopt -s nullglob
        WHEELS=("$WHEELS_DIR"/collimate-*.whl)
        shopt -u nullglob
        if [ "${#WHEELS[@]}" -ne 1 ]; then
            echo "error: expected one collimate wheel in $WHEELS_DIR, found ${#WHEELS[@]}" >&2
            exit 1
        fi
        WHEEL="${WHEELS[0]}"
    else
        echo ""
        echo "[1/3] Carrying $(basename "$WHEEL")..."
    fi
    echo "      Building the server environment (build/server_env.py)..."
    cd "$ROOT_DIR"
    # --release: on a Mac its Mach-O files are signed as the app will be
    # (the Developer ID when MAC_SIGNING is true, else ad-hoc) before the
    # manifest hashes the tree; electron-builder leaves them as they are.
    "$PYTHON" "$ROOT_DIR/build/server_env.py" --wheel "$WHEEL" --out "$SERVER_DIR" $RELEASE
    SERVER_ARGS=(--server "$SERVER_DIR")
else
    echo ""
    echo "[1/3] No server to build (Windows is connect-only)."
    SERVER_ARGS=()
fi
"$PYTHON" "$ROOT_DIR/build/desktop_packaging.py" config --platform "$PLATFORM" --commit "$COMMIT" "$TREE_FLAG" \
    --built-at "$BUILT_AT" "${SERVER_ARGS[@]}" $RELEASE --out "$CONFIG"

# ---------------------------------------------------------------------------
# 2. The shell bundle → desktop/dist/
#
# Reuse only a successful installation newer than both dependency manifests.
# ---------------------------------------------------------------------------
echo ""
echo "[2/3] Building the shell bundle..."
cd "$DESKTOP_DIR"
bash "$ROOT_DIR/build/npm_ci_if_stale.sh" "$DESKTOP_DIR"
npm run build          # esbuild → desktop/dist/{main.cjs,preload.cjs,splash/}

# ---------------------------------------------------------------------------
# 3. Package → desktop/release/
#
# The whole directory goes first, so nothing from an earlier run is mistaken
# for this one's. `--publish never` unconditionally: this script builds
# artifacts, it never uploads them (release.yml's release job does).
# ---------------------------------------------------------------------------
echo ""
echo "[3/3] Packaging with electron-builder..."
rm -rf "$RELEASE_DIR"
npx --no-install electron-builder --config "$CONFIG" "${TARGET_ARGS[@]}" --publish never "${PASSTHROUGH[@]}"

echo ""
# Installers only — the unpacked *-unpacked/ trees and electron-builder's
# own metadata (builder-effective-config.yaml) are noise here.
# A --dir build has none of them, and an empty list under a heading reads
# like something went wrong, so say what actually happened instead.
ARTIFACTS=()
while IFS= read -r -d '' artifact; do
    ARTIFACTS+=("$artifact")
done < <(find "$RELEASE_DIR" -maxdepth 1 -type f \
    \( -name '*.dmg' -o -name '*.zip' -o -name '*.AppImage' -o -name '*.deb' \
       -o -name '*.exe' -o -name '*.rpm' -o -name '*.snap' \) -print0)
if [ "${#ARTIFACTS[@]}" -gt 0 ]; then
    echo "  Installers in desktop/release/:"
    for artifact in "${ARTIFACTS[@]}"; do
        # ls -l's size field precedes the path, so a space in the path
        # cannot shift it.
        size="$(ls -lhd -- "$artifact" | awk '{ print $5 }')"
        printf '    %-10s %s\n' "$size" "$(basename "$artifact")"
    done
else
    echo "  No installers (--dir build) — the unpacked app is in desktop/release/."
fi

# A built AppImage also lands in one known place, under electron-builder's
# standard name (Collimate-<version>-linux-x86_64.AppImage), replacing the
# same name from an earlier build: the file you run or hand to someone is
# always the latest one built, and desktop/release/ can be wiped freely.
APPIMAGES=()
while IFS= read -r -d '' appimage; do
    APPIMAGES+=("$appimage")
done < <(find "$RELEASE_DIR" -maxdepth 1 -type f -name '*.AppImage' -print0)
if [ "${#APPIMAGES[@]}" -gt 0 ]; then
    mkdir -p "$APPIMAGE_DIR"
    for appimage in "${APPIMAGES[@]}"; do
        cp -f "$appimage" "$APPIMAGE_DIR/$(basename "$appimage")"
        chmod +x "$APPIMAGE_DIR/$(basename "$appimage")"
        echo "  AppImage copied to $APPIMAGE_DIR/$(basename "$appimage")"
    done
fi
echo ""
