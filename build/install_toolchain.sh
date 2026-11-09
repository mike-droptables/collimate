#!/usr/bin/env bash
# install_toolchain.sh — Check the tools a checkout builds with; install or update them.
# Usage: bash build/install_toolchain.sh [--check]
#   --check     report only: change nothing, exit 1 if anything is missing or too
#               old on the caller's own PATH (setup.sh runs this after installing)
#   -h|--help   print this usage
#
# The requirements are README.md's, at the versions CI's setup-toolchain
# action pins (.github/actions/setup-toolchain/action.yml):
#   git        required; installed by the OS package manager, never by this script
#   uv         installed with Astral's standalone installer; a standalone uv is
#              updated with `uv self update` (at most daily), a snap/brew/pip uv
#              by its manager
#   Python     ≥ 3.13 as python3 (build scripts call it directly); installed by uv
#   Node       24.x, ≥ 24.15, with npm; installed through nvm/fnm/volta when one
#              is present, Homebrew on macOS, else the official nodejs.org
#              tarball (checksum-verified) under ~/.local/share/node, linked
#              into ~/.local/bin
# Nothing here needs sudo. ./setup.sh runs this first, then installs the
# project's packages.
set -euo pipefail

CHECK_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        -h|--help)
            sed -n '2,/^set -e/p' "${BASH_SOURCE[0]}" | sed '$d'
            exit 0
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            echo "       Usage: bash build/install_toolchain.sh [--check]" >&2
            exit 2
            ;;
    esac
    shift
done

PYTHON_MIN="3.13"
NODE_MAJOR=24
NODE_MIN="24.15.0"
LOCAL_BIN="$HOME/.local/bin"
NODE_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/node"

# --check judges the PATH it was given, the one setup.sh and the build scripts
# run with. An install puts ~/.local/bin first so it verifies what it installs
# there, then says where the caller's PATH would find something else.
CALLER_PATH="$PATH"
if [ "$CHECK_ONLY" = 0 ]; then
    export PATH="$LOCAL_BIN:$PATH"
fi

PROBLEMS=0
ok()   { printf '  ok      %s\n' "$*"; }
info() { printf '  ...     %s\n' "$*"; }
bad()  { printf '  MISSING %s\n' "$*" >&2; PROBLEMS=$((PROBLEMS + 1)); }

# shadowed TOOL: why a copy in ~/.local/bin isn't the one PATH resolves.
shadowed() {
    [ -x "$LOCAL_BIN/$1" ] || return 0
    printf ' — %s is installed, but PATH resolves %s; put ~/.local/bin ahead on PATH' \
        "$LOCAL_BIN/$1" "$(command -v "$1" || echo nothing)"
}

# version_ge A B: true when dotted version A ≥ B.
version_ge() {
    [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

fetch() {
    if command -v curl >/dev/null 2>&1; then curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then wget -qO- "$1"
    else
        echo "error: neither curl nor wget is installed; install one and rerun" >&2
        exit 1
    fi
}

os_install_hint() {
    case "$(uname -s)" in
        Darwin) echo "brew install $1" ;;
        *)
            if command -v apt-get >/dev/null 2>&1; then echo "sudo apt-get install $1"
            elif command -v dnf >/dev/null 2>&1; then echo "sudo dnf install $1"
            elif command -v pacman >/dev/null 2>&1; then echo "sudo pacman -S $1"
            else echo "install $1 with your package manager"
            fi
            ;;
    esac
}

# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
echo "git"
if command -v git >/dev/null 2>&1; then
    ok "$(git --version)"
else
    bad "git — $(os_install_hint git)"
fi

# ---------------------------------------------------------------------------
# uv
# ---------------------------------------------------------------------------
echo "uv"
if ! command -v uv >/dev/null 2>&1; then
    if [ "$CHECK_ONLY" = 1 ]; then
        bad "uv — not on PATH$(shadowed uv)"
    else
        info "installing uv (astral.sh/uv/install.sh)"
        fetch https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
        hash -r
        command -v uv >/dev/null 2>&1 && ok "$(uv --version)" || bad "uv — install failed"
    fi
else
    uv_path="$(command -v uv)"
    case "$uv_path" in
        /snap/*)                            manager="snap (refreshes itself; sudo snap refresh astral-uv)" ;;
        */Cellar/*|/opt/homebrew/*)         manager="brew (brew upgrade uv)" ;;
        */site-packages/*|*/.venv/*|*/pipx/*) manager="pip (pip install -U uv)" ;;
        *)                                  manager="" ;;
    esac
    # A standalone uv updates itself, at most once a day: setup.sh runs this on
    # every rerun, and an older uv still builds (a too-old one fails loudly).
    uv_stamp="${XDG_CACHE_HOME:-$HOME/.cache}/collimate/uv-self-update"
    if [ -z "$manager" ] && [ "$CHECK_ONLY" = 0 ] \
       && [ -z "$(find "$uv_stamp" -mmin -1440 2>/dev/null)" ] \
       && uv self update >/dev/null 2>&1; then
        mkdir -p "${uv_stamp%/*}" && touch "$uv_stamp"
        ok "$(uv --version) (self-updated)"
    else
        ok "$(uv --version)${manager:+ — managed by $manager}"
    fi
fi

# ---------------------------------------------------------------------------
# Python ≥ 3.13 as python3
# ---------------------------------------------------------------------------
echo "python"
python_version() { python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null; }
py="$(python_version || true)"
if [ -n "$py" ] && version_ge "$py" "$PYTHON_MIN"; then
    ok "python3 $py"
elif [ "$CHECK_ONLY" = 1 ] || ! command -v uv >/dev/null 2>&1; then
    bad "python3 ≥ $PYTHON_MIN (found: ${py:-none})$(shadowed python3)"
else
    info "installing Python $PYTHON_MIN with uv as the default python3 (found: ${py:-none})"
    uv python install "$PYTHON_MIN" --default
    hash -r
    py="$(python_version || true)"
    if [ -n "$py" ] && version_ge "$py" "$PYTHON_MIN"; then
        ok "python3 $py"
    else
        bad "python3 ≥ $PYTHON_MIN — uv installed it into $LOCAL_BIN, but python3 still resolves to $(command -v python3 || echo nothing)"
    fi
fi

# ---------------------------------------------------------------------------
# Node 24.x ≥ 24.15 and npm
# ---------------------------------------------------------------------------
echo "node"
node_version() { node --version 2>/dev/null | sed 's/^v//'; }
node_ok() {
    local v="$1"
    [ -n "$v" ] && [ "${v%%.*}" = "$NODE_MAJOR" ] && version_ge "$v" "$NODE_MIN"
}

install_node_tarball() {
    local os arch base file sums want
    case "$(uname -s)" in
        Linux) os=linux ;;
        Darwin) os=darwin ;;
        *) echo "error: no Node tarball for $(uname -s)" >&2; return 1 ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) arch=x64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) echo "error: no Node tarball for $(uname -m)" >&2; return 1 ;;
    esac
    base="https://nodejs.org/dist/latest-v${NODE_MAJOR}.x"
    sums="$(fetch "$base/SHASUMS256.txt")"
    file="$(printf '%s\n' "$sums" | awk -v want="-$os-$arch.tar.gz" '$2 ~ want"$" {print $2; exit}')"
    want="$(printf '%s\n' "$sums" | awk -v f="$file" '$2 == f {print $1}')"
    [ -n "$file" ] && [ -n "$want" ] || { echo "error: no $os-$arch tarball in $base" >&2; return 1; }

    local dir="$NODE_HOME/${file%.tar.gz}"
    if [ ! -x "$dir/bin/node" ]; then
        local tmp got
        tmp="$(mktemp -d)"
        info "downloading $file"
        fetch "$base/$file" > "$tmp/$file"
        if command -v sha256sum >/dev/null 2>&1; then got="$(sha256sum "$tmp/$file" | awk '{print $1}')"
        else got="$(shasum -a 256 "$tmp/$file" | awk '{print $1}')"
        fi
        [ "$got" = "$want" ] || { rm -rf "$tmp"; echo "error: checksum mismatch for $file" >&2; return 1; }
        mkdir -p "$NODE_HOME"
        tar -xzf "$tmp/$file" -C "$tmp"
        rm -rf "$dir"
        mv "$tmp/${file%.tar.gz}" "$dir"
        rm -rf "$tmp"
    fi
    mkdir -p "$LOCAL_BIN"
    local tool
    for tool in node npm npx corepack; do
        [ -e "$dir/bin/$tool" ] && ln -sfn "$dir/bin/$tool" "$LOCAL_BIN/$tool"
    done
    # Older tarball installs are superseded by the links above.
    find "$NODE_HOME" -mindepth 1 -maxdepth 1 -type d -name "node-v${NODE_MAJOR}.*" ! -path "$dir" -exec rm -rf {} +
}

install_node() {
    if [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
        info "installing Node $NODE_MAJOR with nvm"
        # nvm is a shell function; it changes this script's PATH, not the caller's.
        set +u
        # shellcheck disable=SC1091
        . "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
        nvm install "$NODE_MAJOR" && nvm alias default "$NODE_MAJOR"
        set -u
    elif command -v fnm >/dev/null 2>&1; then
        info "installing Node $NODE_MAJOR with fnm"
        fnm install "$NODE_MAJOR" && fnm default "$NODE_MAJOR"
        eval "$(fnm env)"
    elif command -v volta >/dev/null 2>&1; then
        info "installing Node $NODE_MAJOR with volta"
        volta install "node@$NODE_MAJOR"
    elif [ "$(uname -s)" = Darwin ] && command -v brew >/dev/null 2>&1; then
        info "installing node@$NODE_MAJOR with Homebrew"
        brew install "node@$NODE_MAJOR" || brew upgrade "node@$NODE_MAJOR"
        brew link --overwrite --force "node@$NODE_MAJOR"
    else
        info "installing the latest Node $NODE_MAJOR.x from nodejs.org into $NODE_HOME"
        install_node_tarball
    fi
    hash -r
}

nv="$(node_version || true)"
if node_ok "$nv"; then
    ok "node $nv"
elif [ "$CHECK_ONLY" = 1 ]; then
    bad "node $NODE_MAJOR.x ≥ $NODE_MIN (found: ${nv:-none})$(shadowed node)"
else
    info "node $NODE_MAJOR.x ≥ $NODE_MIN needed (found: ${nv:-none})"
    install_node
    nv="$(node_version || true)"
    if node_ok "$nv"; then
        ok "node $nv"
    else
        bad "node $NODE_MAJOR.x ≥ $NODE_MIN — installed, but node resolves to $(command -v node || echo nothing) (${nv:-none})"
    fi
fi
if command -v npm >/dev/null 2>&1; then
    ok "npm $(npm --version)"
else
    bad "npm — it ships with Node; reinstall Node $NODE_MAJOR"
fi

# ---------------------------------------------------------------------------
# The caller's PATH: where it resolves a tool other than the one verified
# ---------------------------------------------------------------------------
stale=()
if [ "$CHECK_ONLY" = 0 ]; then
    in_local=0
    for tool in uv python3 node npm; do
        here="$(command -v "$tool" || true)"
        there="$(PATH="$CALLER_PATH"; hash -r; command -v "$tool" || true)"
        if [ -n "$here" ] && [ "$here" != "$there" ]; then
            stale+=("$tool: ${there:-not found} (verified: $here)")
            case "$here" in "$LOCAL_BIN"/*) in_local=1 ;; esac
        fi
    done
    if [ "${#stale[@]}" -gt 0 ]; then
        echo ""
        echo "note: your shell's PATH finds other copies:"
        printf '        %s\n' "${stale[@]}"
        if [ "$in_local" = 1 ]; then
            echo "      Put ~/.local/bin first in your shell's startup file (~/.bashrc, and"
            echo "      ~/.bash_profile or ~/.profile for login shells):"
            echo "        export PATH=\"\$HOME/.local/bin:\$PATH\""
        fi
        echo "      Then open a new shell before ./setup.sh or the build scripts."
    fi
fi

echo ""
if [ "$PROBLEMS" -gt 0 ]; then
    echo "$PROBLEMS requirement(s) unmet." >&2
    exit 1
fi
if [ "${#stale[@]}" -gt 0 ]; then
    echo "Toolchain installed; this shell's PATH needs the fix above."
else
    echo "Toolchain ready."
fi
