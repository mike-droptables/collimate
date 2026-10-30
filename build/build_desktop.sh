#!/bin/bash
# build_desktop.sh — Build, package and install this checkout's desktop app.
# Usage: bash build/build_desktop.sh [flags] [-- electron-builder arguments]
#   --skip              reuse api/static; always rebuild the shell
#   --wipe              remove stable profile channels before building
#   --no-install        package only (default target: unpacked --dir)
#   --install-dir DIR   override the per-user installation directory
#   --uninstall         remove the installed app and serverSpec; no build
#   --reset             build/package, uninstall/install, then reset defaults
#                       stop the app's server first; incompatible with --wipe,
#                       --no-install, --uninstall and --apparmor
#   --apparmor          load the Linux sandbox profiles without rebuilding
#   --no-sudo           print required privileged commands instead of running them
#   --linux|--mac|--win|--dir  select electron-builder targets
#   -h|--help           print this usage
# Unknown flags are errors. Packaging always uses --publish never.
# Default install: Linux ~/.local/share/collimate/app; macOS ~/Applications.
# Profiles, launcher/sandbox setup, reset semantics and release rehearsal:
# docs/ai_tools/notes/2026-10-11/project_desktop_dev_install.md
set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$ROOT_DIR/api"
DESKTOP_DIR="$ROOT_DIR/desktop"
RELEASE_DIR="$DESKTOP_DIR/release"

# ---------------------------------------------------------------------------
# Arguments: our own flags vs. everything electron-builder should see.
#
# A while/shift loop rather than `for arg in "$@"` because --install-dir
# takes a value. The flags shared with dev_desktop.sh are handled exactly as
# dev_desktop handles them, so the two can't drift on what --wipe means —
# including the rule that an unrecognised argument is an error. The only
# way to hand electron-builder something this script doesn't know is after
# an explicit `--`, so a mistyped flag can never ride through silently.
# ---------------------------------------------------------------------------
SKIP=0
WIPE=0
INSTALL=1
UNINSTALL=0
RESET=0
APPARMOR_ONLY=0
NO_SUDO=0
INSTALL_DIR=""
BUILDER_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --skip) SKIP=1 ;;
        --wipe) WIPE=1 ;;
        --no-install) INSTALL=0 ;;
        --uninstall) UNINSTALL=1 ;;
        --reset) RESET=1 ;;
        --apparmor) APPARMOR_ONLY=1 ;;
        --no-sudo) NO_SUDO=1 ;;
        --install-dir)
            shift
            [ $# -gt 0 ] || {
                echo "error: --install-dir needs a directory" >&2
                exit 2
            }
            INSTALL_DIR="$1"
            ;;
        --install-dir=*) INSTALL_DIR="${1#--install-dir=}" ;;
        -h|--help)
            # The header block: line 2 through the last comment line before
            # `set -e`. Computed rather than a fixed range so editing the
            # header can't silently truncate --help.
            sed -n '2,/^set -e/p' "${BASH_SOURCE[0]}" | sed '$d'
            exit 0
            ;;
        # The electron-builder targets this script's own usage names. Passed
        # through as-is; anything more exotic goes after `--`.
        --linux|--mac|--win|--dir) BUILDER_ARGS+=("$1") ;;
        --)
            shift
            [ $# -gt 0 ] || {
                echo "error: nothing follows '--' (electron-builder arguments go after it)" >&2
                exit 2
            }
            BUILDER_ARGS+=("$@")
            break
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            echo "       Usage: bash build/build_desktop.sh [--skip] [--wipe] [--no-install] [--install-dir DIR] [--uninstall] [--reset] [--apparmor] [--no-sudo] [--linux|--mac|--win|--dir] [-- <electron-builder args>]" >&2
            echo "       (bash build/build_desktop.sh --help for the full list)" >&2
            exit 2
            ;;
    esac
    shift
done

# SKIP_SERVER_BUILD is just the environment spelling of --skip.
if [ "${SKIP_SERVER_BUILD:-0}" = "1" ]; then
    SKIP=1
fi

# --reset is the install loop plus a data step that KEEPS the channels; the
# flags that delete the data or skip the install contradict it, and the
# stop-early modes are already inside it.
if [ "$RESET" = "1" ]; then
    if [ "$WIPE" = "1" ]; then
        echo "error: --reset keeps the channel data and --wipe deletes it — pick one" >&2
        exit 2
    fi
    if [ "$INSTALL" != "1" ]; then
        echo "error: --reset reinstalls the app; it can't combine with --no-install" >&2
        exit 2
    fi
    if [ "$UNINSTALL" = "1" ] || [ "$APPARMOR_ONLY" = "1" ]; then
        echo "error: --reset already uninstalls and reinstalls — drop --uninstall / --apparmor" >&2
        exit 2
    fi
fi

# ---------------------------------------------------------------------------
# Sandboxed terminal? Refuse before anything is built or touched — see
# sandboxed-terminal guidance in the canonical dev-install note linked above.
# ---------------------------------------------------------------------------
refuse_sandboxed_terminal() {
    local real_home="${SNAP_REAL_HOME:-$HOME}" where="" name val
    local rewritten=() leftovers=()

    # The sandbox's own markers.
    if [ -n "${SNAP:-}" ]; then
        where="the '${SNAP_NAME:-?}' snap${SNAP_REVISION:+ (revision $SNAP_REVISION)}"
    elif [ -n "${FLATPAK_ID:-}" ] || [ -f /.flatpak-info ]; then
        where="the '${FLATPAK_ID:-?}' flatpak"
    fi
    # The symptom: a per-user directory variable pointing into a sandbox's
    # private copy of the home directory.
    for name in HOME XDG_DATA_HOME XDG_CONFIG_HOME XDG_CACHE_HOME XDG_STATE_HOME; do
        val="${!name:-}"
        case "$val" in
            "$real_home"/snap/*|"$real_home"/.var/app/*) rewritten+=("$name=$val") ;;
        esac
    done
    [ -n "$where" ] || [ "${#rewritten[@]}" -gt 0 ] || return 0

    local action="install"
    if [ "$RESET" = "1" ]; then action="reset the installed app"
    elif [ "$UNINSTALL" = "1" ]; then action="uninstall"
    elif [ "$APPARMOR_ONLY" = "1" ]; then action="load the AppArmor profile"
    elif [ "$INSTALL" != "1" ]; then action="wipe channel data"
    fi
    echo "error: refusing to $action from a terminal inside ${where:-a sandbox}." >&2
    if [ "${#rewritten[@]}" -gt 0 ]; then
        echo "       It rewrites the per-user directories this script writes into:" >&2
        for val in "${rewritten[@]}"; do echo "         $val" >&2; done
    else
        echo "       Sandboxes rewrite the per-user directories this script writes into" >&2
        echo "       (HOME, XDG_DATA_HOME, XDG_CONFIG_HOME, …)." >&2
    fi
    echo "       The app tree, launcher entry and icons would land in the sandbox's" >&2
    echo "       private copy of your home directory, which the desktop session never" >&2
    echo "       reads: the script would report success and nothing would appear in" >&2
    echo "       the app menu." >&2
    echo "" >&2
    echo "       Run the same command from a terminal emulator outside the sandbox" >&2
    echo "       (not the editor's integrated terminal)." >&2

    # What an earlier run from in here left behind, so it can be cleaned up:
    # --uninstall is refused here for the same reason, and from outside it
    # looks in the real ~/.local/share.
    local data_home="${XDG_DATA_HOME:-$HOME/.local/share}" bin_link="$HOME/.local/bin/collimate"
    case "$data_home" in
        "$real_home"/snap/*|"$real_home"/.var/app/*)
            [ -d "$data_home/collimate" ] && leftovers+=("$data_home/collimate")
            [ -e "$data_home/applications/collimate.desktop" ] && leftovers+=("$data_home/applications/collimate.desktop")
            for val in "$data_home"/icons/hicolor/*/apps/collimate.*; do
                [ -e "$val" ] || continue
                leftovers+=("$data_home/icons/hicolor/*/apps/collimate.{png,svg}")
                break
            done
            if [ -L "$bin_link" ] && [[ "$(readlink "$bin_link")" == "$data_home"/* ]]; then
                leftovers+=("$bin_link")
            fi
            ;;
    esac
    if [ "${#leftovers[@]}" -gt 0 ]; then
        echo "" >&2
        echo "       An earlier run from in here left an install in the sandbox's copy," >&2
        echo "       which nothing outside reads. Delete it by hand (rm -rf):" >&2
        for val in "${leftovers[@]}"; do echo "         $val" >&2; done
    fi
    exit 2
}
if [ "$INSTALL" = "1" ] || [ "$UNINSTALL" = "1" ] || [ "$APPARMOR_ONLY" = "1" ] || [ "$WIPE" = "1" ]; then
    refuse_sandboxed_terminal
fi

# Fail fast with a clear message if required tools aren't installed.
# uv isn't checked here: the server build doesn't use it, and the shell
# provisions its own uv at runtime.
for tool in node npm npx; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "error: '$tool' not found on PATH — see README.md prerequisites" >&2
        exit 1
    }
done
if [ "$RESET" = "1" ]; then
    # The reset step runs build/reset_defaults.py through uv whether or not
    # the server build (which checks uv itself) ran.
    command -v uv >/dev/null 2>&1 || {
        echo "error: 'uv' not found on PATH — --reset runs build/reset_defaults.py through it" >&2
        exit 1
    }
fi

APP_VERSION="$(node -p "require('$DESKTOP_DIR/package.json').version")"

# Where the installed app keeps everything: Electron's user-data dir for the
# app name "Collimate" (index.ts sets it before any getPath call, from the
# 'stable' profile every packaged app runs). Both the wipe and the
# settings.json write below live here.
#
# Pinned to stable rather than following COLLIMATE_APP_PROFILE, unlike
# dev_desktop.sh's copy of this. Both scripts obey the same rule — aim at the
# data dir of the app this script is about to produce — and what this one
# produces is a packaged app, whose profile is fixed. The variable as set in
# the terminal running this script does not follow the installed app to a
# launcher click, so honouring it here would write serverSpec into a directory
# that app never reads, leaving it to try a PyPI version that does not exist
# yet. A dev_desktop.sh run, by contrast, inherits the terminal's environment
# directly, which is why its wipe does honour it.
#
# POSIX only. On Windows the real path is %APPDATA%\Collimate, and the shell
# is connect-only there anyway (no spawn, so no serverSpec to write); the
# install step bails out before any of this matters.
case "$(uname -s)" in
    Darwin) USER_DATA="$HOME/Library/Application Support/Collimate" ;;
    *)      USER_DATA="${XDG_CONFIG_HOME:-$HOME/.config}/Collimate" ;;
esac

# ---------------------------------------------------------------------------
# Before 0: while the installed app's server is running, --wipe, --uninstall and
#     --reset are refused before anything is touched: the wipe unlinks the
#     databases a running server keeps writing into, the uninstall pulls the
#     app tree from under it, and --reset's data step at the end rewrites its
#     channel state. The shell records the server it spawned in
#     <user-data dir>/server.json ({pid, port, url, startedAt, version,
#     shellVersion, spec, dataDir}, removed on a clean stop and kept when the
#     app quits with the server left running, for the next launch to adopt —
#     desktop/src/main/server.ts); a record naming a dead pid is a crash
#     leftover and does not count. A kept-running server outlives the app,
#     so "the app is closed" does not mean "the data dir is free". build/reset_defaults.py then try-locks
#     <user-data dir>/.collimate/server.lock, which every running server
#     holds whatever launched it, and holds it for the whole reset.
# ---------------------------------------------------------------------------
live_server_pid() {
    local record="$1/server.json" pid
    [ -f "$record" ] || return 1
    pid="$(node -e '
let pid = 0;
try {
  const r = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
  if (r && Number.isInteger(r.pid) && r.pid > 1) pid = r.pid;
} catch {}
console.log(pid);
' "$record" 2>/dev/null || echo 0)"
    [ "$pid" != "0" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}
if [ "$WIPE" = "1" ] || [ "$UNINSTALL" = "1" ] || [ "$RESET" = "1" ]; then
    if LIVE_PID="$(live_server_pid "$USER_DATA")"; then
        if [ "$RESET" = "1" ]; then what="--reset rewrites that app's channel state"
        elif [ "$WIPE" = "1" ]; then what="--wipe deletes that app's channel data"
        else what="--uninstall removes that app from under its server"
        fi
        echo "error: Collimate's server is running (server pid $LIVE_PID, recorded in $USER_DATA/server.json)." >&2
        echo "       $what — stop the local server first: File → Servers → Stop, or quit the app" >&2
        echo "       without keeping the server running (open it again if it was kept running at quit)." >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 0. Wipe, if asked. Before everything else, so a failed build leaves the
#    app in the state the flag asked for rather than half of it.
#
#    Only .collimate/, ever: settings.json (which carries the serverSpec
#    this script writes) and logs/ stay.
# ---------------------------------------------------------------------------
if [ "$WIPE" = "1" ] && [ -n "$USER_DATA" ]; then
    target="$USER_DATA/.collimate"
    if [ -d "$target" ]; then
        echo "[build_desktop] wiping $target"
        rm -rf "$target"
    fi
fi

# ---------------------------------------------------------------------------
# Target detection: electron-builder is told --dir unless the caller named a
# target themselves.
#
# --dir stops at the unpacked tree, which is both what the install step
# copies and the only part of a Linux/macOS build that isn't minutes of
# appimagetool/hdiutil. Ask for --linux (or --mac, or --win) and you get the
# installers exactly as before — the unpacked tree is produced on the way to
# them, so the install step still works.
# ---------------------------------------------------------------------------
HAS_TARGET=0
for arg in "${BUILDER_ARGS[@]}"; do
    case "$arg" in
        --dir|--linux*|--mac*|--win*|-l|-m|-w|-lm|-lw|-mw|-lmw) HAS_TARGET=1 ;;
    esac
done
if [ "$HAS_TARGET" = "0" ]; then
    BUILDER_ARGS+=(--dir)
fi

echo "=================================="
echo "  Build: Collimate desktop shell  "
echo "=================================="
echo ""
echo "  version:  $APP_VERSION"
if [ "$INSTALL" = "1" ]; then
    echo "  server:   uv run python main.py in $API_DIR   (this checkout, via settings.json serverSpec)"
else
    echo "  server:   uvx --from collimate==$APP_VERSION collimate   (PyPI, at runtime)"
fi
echo "  targets:  ${BUILDER_ARGS[*]}"
if [ "$INSTALL" = "1" ]; then
    echo "  install:  yes (--no-install to skip)"
else
    echo "  install:  no"
fi
if [ "$RESET" = "1" ]; then
    echo "  reset:    every channel's capability cells from the bundled content → this build ($USER_DATA)"
fi

# ---------------------------------------------------------------------------
# Linux helpers shared by the install, --uninstall and --apparmor paths.
# ---------------------------------------------------------------------------
APPARMOR_DEST="/etc/apparmor.d/collimate"

# The per-user install locations. One function so the three paths that
# need them can't drift on where the app lives.
linux_paths() {
    DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
    APP_DIR="${INSTALL_DIR:-$DATA_HOME/collimate/app}"
    APPS_DIR="$DATA_HOME/applications"
    ICON_ROOT="$DATA_HOME/icons/hicolor"
    DESKTOP_FILE="$APPS_DIR/collimate.desktop"
    BIN_LINK="$HOME/.local/bin/collimate"
}

# Menu and icon caches. All best-effort: a desktop environment that
# watches the directories (most do) doesn't need them, and one that
# doesn't will catch up at the next login. KDE's is its service database
# (sycoca), which Plasma's launcher and task manager read entries from; a
# rebuild makes them pick up a changed Icon= now rather than at next login.
refresh_desktop_caches() {
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t "$ICON_ROOT" >/dev/null 2>&1 || true
    fi
    local sycoca
    for sycoca in kbuildsycoca6 kbuildsycoca5; do
        command -v "$sycoca" >/dev/null 2>&1 || continue
        "$sycoca" --noincremental >/dev/null 2>&1 || true
        break
    done
}

# The executable inside an installed/unpacked tree: package.json's `name`
# (electron-builder's default executableName — see the install step), the
# historical spellings, then the one executable next to resources/.
find_app_bin() {
    local tree="$1" candidate base
    local pkg_name
    pkg_name="$(node -p "require('$DESKTOP_DIR/package.json').name" 2>/dev/null || echo "")"
    for candidate in "$pkg_name" collimate-desktop collimate Collimate; do
        if [ -n "$candidate" ] && [ -f "$tree/$candidate" ] && [ -x "$tree/$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    for candidate in "$tree"/*; do
        base="$(basename "$candidate")"
        case "$base" in
            chrome-sandbox|chrome_crashpad_handler|*.so|*.so.*|*.pak|*.bin|*.dat|*.html|*.txt|*.json|*.png|*.svg)
                continue
                ;;
        esac
        if [ -f "$candidate" ] && [ -x "$candidate" ]; then
            echo "$base"
            return 0
        fi
    done
    return 1
}

userns_restricted() {
    [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = "1" ] && return 0
    [ "$(cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null || echo 1)" = "0" ] && return 0
    return 1
}

# Write the profile text for this install to $1; see the canonical dev-install
# note for Chromium sandbox setup. Same shape as /etc/apparmor.d/obsidian and code.
write_apparmor_profile() {
    local out="$1" app_bin="$2" dev_bin="$ROOT_DIR/desktop/node_modules/electron/dist/electron"
    mkdir -p "$(dirname "$out")"
    cat > "$out" <<EOF
# Collimate — written by build/build_desktop.sh. Same shape as the profiles
# Ubuntu ships for Chrome, VS Code and Obsidian: it gives the app a name that
# may create user namespaces (Chromium's sandbox) on a system with
# kernel.apparmor_restrict_unprivileged_userns=1. Reinstalling the app keeps
# the path, so this stays valid; \`build/build_desktop.sh --uninstall\`
# removes it.
abi <abi/4.0>,
include <tunables/global>

profile collimate "$app_bin" flags=(unconfined) {
  userns,

  # Site-specific additions and overrides. See local/README for details.
  include if exists <local/collimate>
}

# The dev run (build/dev_desktop.sh) is the checkout's own Electron binary.
profile collimate-dev "$dev_bin" flags=(unconfined) {
  userns,
}
EOF
}

apparmor_print_manual() {
    echo "      To load it by hand (one time, survives reinstalls):"
    echo "        sudo install -m 0644 \"$1\" $APPARMOR_DEST"
    echo "        sudo apparmor_parser -r $APPARMOR_DEST"
    echo "      or: bash build/build_desktop.sh --apparmor"
}

# Load the profile at $1 into /etc/apparmor.d — with sudo, after saying why,
# and only when a terminal can answer the password prompt.
apparmor_apply() {
    local src="$1"
    if [ -f "$APPARMOR_DEST" ] && cmp -s "$src" "$APPARMOR_DEST"; then
        echo "      apparmor       $APPARMOR_DEST (already current)"
        return 0
    fi
    if [ "$NO_SUDO" = "1" ] || [ ! -t 0 ] || ! command -v sudo >/dev/null 2>&1; then
        echo "      apparmor       NOT loaded: Chromium's sandbox needs $APPARMOR_DEST on this"
        echo "                     system (unprivileged user namespaces are restricted);"
        echo "                     without it the app aborts at startup. Profile written to"
        echo "                     $src"
        apparmor_print_manual "$src"
        return 0
    fi
    echo "      Chromium's sandbox needs an AppArmor profile on this system (unprivileged"
    echo "      user namespaces are restricted); without it the app aborts at startup."
    echo "      Loading $APPARMOR_DEST — sudo will ask for your password:"
    if sudo install -m 0644 "$src" "$APPARMOR_DEST" && sudo apparmor_parser -r "$APPARMOR_DEST"; then
        echo "      apparmor       $APPARMOR_DEST (loaded)"
    else
        echo "      warning: the profile was not loaded" >&2
        apparmor_print_manual "$src"
    fi
}

apparmor_remove() {
    [ -f "$APPARMOR_DEST" ] || { echo "      apparmor       (none)"; return 0; }
    if [ "$NO_SUDO" = "1" ] || [ ! -t 0 ] || ! command -v sudo >/dev/null 2>&1; then
        echo "      apparmor       $APPARMOR_DEST left in place — remove it with:"
        echo "        sudo apparmor_parser -R $APPARMOR_DEST && sudo rm -f $APPARMOR_DEST"
        return 0
    fi
    echo "      Removing $APPARMOR_DEST — sudo will ask for your password:"
    if sudo apparmor_parser -R "$APPARMOR_DEST" && sudo rm -f "$APPARMOR_DEST"; then
        echo "      apparmor       removed $APPARMOR_DEST"
    else
        echo "      warning: $APPARMOR_DEST was not removed; do it with:" >&2
        echo "        sudo apparmor_parser -R $APPARMOR_DEST && sudo rm -f $APPARMOR_DEST" >&2
    fi
}

# ---------------------------------------------------------------------------
# 0b. Uninstall, if asked — and stop there. Runs after the wipe so
#     `--uninstall --wipe` means "take it all away" in one go.
#
#     Every removal is guarded the way the install's own rm -rf is: the app
#     tree only when it carries resources/app.asar (a Collimate install, not
#     someone's other folder), the symlink only when it points into that
#     tree, and settings.json is read-modify-written so the shell's other
#     keys (saved servers, their tokens, keepLocalRunning) survive.
# ---------------------------------------------------------------------------
remove_server_spec() {
    local file="$1/settings.json"
    [ -f "$file" ] || { echo "      settings.json  (no file — nothing to drop)"; return 0; }
    node -e '
const fs = require("node:fs");
const [file] = process.argv.slice(1);
let data;
try { data = JSON.parse(fs.readFileSync(file, "utf8")); } catch { process.exit(0); }
if (!data || typeof data !== "object" || Array.isArray(data) || !("serverSpec" in data)) {
  console.log("      settings.json  (no serverSpec key — nothing to drop)");
  process.exit(0);
}
delete data.serverSpec;
fs.writeFileSync(file, JSON.stringify(data, null, 2) + "\n");
console.log("      settings.json  serverSpec dropped (the app would now run the PyPI release)");
' "$file"
}

if [ "$RESET" = "1" ]; then
    case "$(uname -s)" in
        Linux|Darwin) ;;
        *)
            echo "error: --reset needs the install automation, which $(uname -s) doesn't have" >&2
            echo "       (see the install step: use the NSIS installer there)" >&2
            exit 1
            ;;
    esac
fi

uninstall_app() {
    if [ "$RESET" = "1" ]; then
        echo "[reset] Removing the installed Collimate app after successful packaging..."
    else
        echo "[uninstall] Removing the installed Collimate app..."
    fi
    case "$(uname -s)" in
    Linux)
        linux_paths

        if [ -d "$APP_DIR" ]; then
            if [ -e "$APP_DIR/resources/app.asar" ]; then
                rm -rf "$APP_DIR"
                # The default parent ($DATA_HOME/collimate) is ours too — drop
                # it when the app tree was the only thing in it.
                rmdir "$DATA_HOME/collimate" 2>/dev/null || true
                echo "      app            removed $APP_DIR"
            else
                echo "      warning: $APP_DIR has no resources/app.asar, so it isn't a" >&2
                echo "               Collimate install — left alone" >&2
            fi
        else
            echo "      app            (not installed at $APP_DIR)"
        fi

        if [ -f "$DESKTOP_FILE" ]; then
            rm -f "$DESKTOP_FILE"
            echo "      launcher       removed $DESKTOP_FILE"
        else
            echo "      launcher       (none)"
        fi

        ICONS_REMOVED=0
        for f in "$ICON_ROOT"/*/apps/collimate.png "$ICON_ROOT"/*/apps/collimate.svg; do
            [ -e "$f" ] || continue
            rm -f "$f"
            ICONS_REMOVED=$((ICONS_REMOVED + 1))
        done
        echo "      icons          removed $ICONS_REMOVED under $ICON_ROOT"

        if [ -L "$BIN_LINK" ]; then
            LINK_TARGET="$(readlink "$BIN_LINK")"
            case "$LINK_TARGET" in
                "$APP_DIR"/*|"$DATA_HOME/collimate/app"/*)
                    rm -f "$BIN_LINK"
                    echo "      command        removed $BIN_LINK"
                    ;;
                *)
                    if [ ! -e "$BIN_LINK" ]; then
                        rm -f "$BIN_LINK"
                        echo "      command        removed dangling $BIN_LINK"
                    else
                        echo "      warning: $BIN_LINK points elsewhere ($LINK_TARGET) — left alone" >&2
                    fi
                    ;;
            esac
        elif [ -e "$BIN_LINK" ]; then
            echo "      warning: $BIN_LINK exists and is not a symlink — left alone" >&2
        else
            echo "      command        (none)"
        fi

        refresh_desktop_caches

        if [ "$RESET" = "1" ]; then
            # Keyed on the install path, which the reinstall keeps: leaving
            # it loaded spares a sudo prompt here and another at the install.
            echo "      apparmor       kept (same install path after the reinstall)"
        else
            apparmor_remove
        fi
        remove_server_spec "$USER_DATA"
        ;;
    Darwin)
        APP_DEST="${INSTALL_DIR:-$HOME/Applications}/Collimate.app"
        if [ -d "$APP_DEST" ]; then
            rm -rf "$APP_DEST"
            echo "      app            removed $APP_DEST"
        else
            echo "      app            (not installed at $APP_DEST)"
        fi
        remove_server_spec "$USER_DATA"
        ;;
    *)
        echo "      Automated install isn't supported on $(uname -s), so there is nothing"
        echo "      here to remove — use the NSIS uninstaller."
        ;;
    esac
    if [ "$RESET" = "1" ]; then
        echo "      channel data   kept in $USER_DATA (reset after the reinstall)"
    else
        echo ""
        echo "=================================="
        echo "  Uninstalled."
        if [ "$WIPE" = "1" ]; then
            echo "  Channel data was wiped too (--wipe)."
        else
            echo "  Channel data in $USER_DATA is untouched"
            echo "  (bash build/build_desktop.sh --uninstall --wipe removes it too)."
            echo "  A dev_desktop.sh run's own data in \"Collimate Dev\" is untouched either"
            echo "  way — bash build/dev_desktop.sh --wipe is what clears that one."
        fi
        echo "=================================="
        exit 0
    fi
}

# A standalone uninstall still exits before any build.
if [ "$UNINSTALL" = "1" ]; then
    uninstall_app
fi


# ---------------------------------------------------------------------------
# 0c. --apparmor: (re)load the sandbox profile for the current install, and
#     stop there. For the machine that installed before the profile existed,
#     or said no to sudo the first time.
# ---------------------------------------------------------------------------
if [ "$APPARMOR_ONLY" = "1" ]; then
    case "$(uname -s)" in
    Linux)
        linux_paths
        [ -d "$APP_DIR" ] || {
            echo "error: nothing installed at $APP_DIR — run the install first" >&2
            exit 1
        }
        APP_BIN_NAME="$(find_app_bin "$APP_DIR")" || {
            echo "error: no app executable in $APP_DIR" >&2
            exit 1
        }
        echo "[apparmor] Sandbox profile for $APP_DIR/$APP_BIN_NAME"
        write_apparmor_profile "$APP_DIR/apparmor/collimate" "$APP_DIR/$APP_BIN_NAME"
        if ! userns_restricted; then
            echo "      note: this system does not restrict unprivileged user namespaces;"
            echo "            the profile is not needed here, but loading it is harmless."
        fi
        apparmor_apply "$APP_DIR/apparmor/collimate"
        ;;
    *)
        echo "      --apparmor only applies to Linux."
        ;;
    esac
    exit 0
fi

# ---------------------------------------------------------------------------
# 1. Build the server side of the tree → api/static/
#
# Not a payload — none of it goes into the bundle — but it IS what the
# installed app serves, through the local: serverSpec written below. Same
# step, same reason, as build/dev_desktop.sh.
# ---------------------------------------------------------------------------
echo ""
if [ "$SKIP" = "1" ]; then
    echo "[1/4] --skip — leaving api/static/ as it stands"
    # Only an installing run needs api/static to exist: with --no-install
    # nothing here is ever served (the artifacts carry no frontend), so a
    # release-style packaging run must not be blocked by a tree that has
    # never been built.
    if [ "$INSTALL" = "1" ] && [ ! -d "$API_DIR/static" ]; then
        echo "error: --skip but $API_DIR/static is missing — the installed app would" >&2
        echo "       have no UI to serve. Run 'bash build/build_desktop.sh' once" >&2
        echo "       without --skip." >&2
        exit 1
    fi
else
    echo "[1/4] Building the server tree (build/build_server.sh)..."
    bash "$ROOT_DIR/build/build_server.sh"
fi

# ---------------------------------------------------------------------------
# 2. Build the shell bundle → desktop/dist/
#
# Reuse only a successful installation newer than both dependency manifests.
# ---------------------------------------------------------------------------
echo ""
echo "[2/4] Building the shell bundle..."
cd "$DESKTOP_DIR"
bash "$ROOT_DIR/build/npm_ci_if_stale.sh" "$DESKTOP_DIR"
npm run build          # esbuild → desktop/dist/{main.cjs,preload.cjs,splash/}

# ---------------------------------------------------------------------------
# 3. Package → desktop/release/
#
# The whole directory goes first: the install step picks a tree out of it by
# name, and a stale linux-unpacked/ from a previous run would install
# silently and wrongly.
#
# `--publish never` unconditionally: this script builds artifacts, it never
# uploads them. Releasing is the workflow's job (or a deliberate manual
# electron-builder invocation).
# ---------------------------------------------------------------------------
echo ""
echo "[3/4] Packaging with electron-builder..."
rm -rf "$RELEASE_DIR"
npx --no-install electron-builder "${BUILDER_ARGS[@]}" --publish never

echo ""
# Installers only — the unpacked *-unpacked/ trees and electron-builder's
# own metadata (builder-effective-config.yaml, latest*.yml) are noise here.
# A --dir build has none of them, and an empty list under a heading reads
# like something went wrong, so say what actually happened instead.
ARTIFACTS="$(find "$RELEASE_DIR" -maxdepth 1 -type f \
    \( -name '*.dmg' -o -name '*.zip' -o -name '*.AppImage' -o -name '*.deb' \
       -o -name '*.exe' -o -name '*.rpm' -o -name '*.snap' \) \
    -exec ls -lh {} \; | awk '{ printf "    %-10s %s\n", $5, $NF }')"
if [ -n "$ARTIFACTS" ]; then
    echo "  Installers in desktop/release/:"
    echo "$ARTIFACTS"
else
    echo "  No installers (--dir build) — the unpacked app is in desktop/release/."
fi

# ---------------------------------------------------------------------------
# 4. Install on this machine.
# ---------------------------------------------------------------------------
echo ""
if [ "$INSTALL" != "1" ]; then
    echo "[4/4] --no-install — nothing installed."
    echo ""
    echo "=================================="
    echo "  Build complete."
    echo ""
    echo "  Reminder: a packaged app runs 'uvx --from collimate==$APP_VERSION collimate'"
    echo "  on first launch. Publish that version to PyPI before shipping these."
    echo "=================================="
    exit 0
fi

echo "[4/4] Installing on this machine..."

# Merge "serverSpec": "local:<checkout>" into <user-data>/settings.json.
#
# Read-modify-write in node rather than a here-doc that overwrites the file:
# settings.json is the shell's own (saved servers with their tokens and cert
# pins, the server the window opens on, keepLocalRunning, the local port —
# see desktop/src/main/connect.ts), and clobbering a
# saved token to install an app would be a nasty surprise. The script is
# passed as argv so no path ever goes through shell interpolation into JS.
write_server_spec() {
    local settings_dir="$1"
    mkdir -p "$settings_dir"
    node -e '
const fs = require("node:fs");
const [file, spec] = process.argv.slice(1);
let data = {};
try {
  const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) data = parsed;
} catch {
  // No settings file yet (or an unreadable one): start from an empty object.
  // The shell treats an unparseable settings.json as absent too.
}
data.serverSpec = spec;
fs.writeFileSync(file, JSON.stringify(data, null, 2) + "\n");
' "$settings_dir/settings.json" "local:$ROOT_DIR"
    echo "      settings.json  serverSpec = local:$ROOT_DIR"
}

# The closing banner both install branches print. Its arguments are the
# launch hint, one line each; the placeholder-icon note is Linux's alone.
print_installed_banner() {
    local line
    echo ""
    echo "=================================="
    echo "  Installed."
    echo ""
    for line in "$@"; do echo "  $line"; done
    echo ""
    echo "  It serves THIS checkout: $ROOT_DIR"
    echo "  (settings.json serverSpec = local:$ROOT_DIR — 'uv run python main.py'"
    echo "  in $API_DIR). A 'git pull' plus 'bash build/build_server.sh' updates"
    echo "  the app; only shell changes need this script again."
    echo ""
    echo "  A dev_desktop.sh run can stay open beside it: that one is \"Collimate Dev\","
    echo "  a separate instance with its own user-data dir, server and channels, so"
    echo "  by default neither app sees the other's data and neither one's --wipe"
    echo "  touches it (COLLIMATE_APP_PROFILE=stable is the one way to cross over)."
    if [ "$(uname -s)" = Linux ]; then
        echo "  If your shell still shows a placeholder icon for it, the shell cached the"
        echo "  icon theme before this install — restart the shell (dms restart, …)."
    fi
    echo ""
    echo "  Uninstall:  bash build/build_desktop.sh --uninstall   (add --wipe"
    echo "              to delete the channel data as well)"
    echo "=================================="
}

case "$(uname -s)" in
# ---------------------------------------------------------------------------
Linux)
    # Find the unpacked tree electron-builder just wrote. x64 is
    # linux-unpacked; other arches get linux-<arch>-unpacked.
    UNPACKED=""
    for candidate in "$RELEASE_DIR/linux-unpacked" "$RELEASE_DIR"/linux-*-unpacked; do
        if [ -d "$candidate" ]; then
            UNPACKED="$candidate"
            break
        fi
    done
    [ -n "$UNPACKED" ] || {
        echo "error: no unpacked app in $RELEASE_DIR — did electron-builder target linux?" >&2
        exit 1
    }
    # The executable is named after desktop/package.json's `name` — NOT after
    # productName: electron-builder's LinuxPackager defaults executableName to
    # appInfo.sanitizedName.toLowerCase(), and appInfo.name is metadata.name
    # (app-builder-lib/out/linuxPackager.js, out/appInfo.js), so this tree
    # ships `collimate-desktop`. find_app_bin derives it rather than
    # hard-coding it, so renaming the package or setting executableName in
    # electron-builder.yml can't turn a minutes-long packaging run into a
    # dead end here.
    APP_BIN_NAME="$(find_app_bin "$UNPACKED")" || {
        echo "error: no app executable in $UNPACKED" >&2
        exit 1
    }

    linux_paths

    # This script rm -rf's APP_DIR, and --install-dir means a user can name
    # it. Two guards, because an unlucky path here deletes someone's home:
    # never a path that is obviously not ours, and never a non-empty
    # directory that doesn't look like a previous install of this app.
    case "$APP_DIR" in
        ""|"/"|"$HOME"|"$HOME/")
            echo "error: refusing to install into '$APP_DIR'" >&2
            exit 2
            ;;
    esac
    if [ -e "$APP_DIR" ] && [ ! -d "$APP_DIR" ]; then
        echo "error: $APP_DIR exists and is not a directory" >&2
        exit 2
    fi
    if [ -d "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ] \
       && [ ! -e "$APP_DIR/resources/app.asar" ]; then
        echo "error: $APP_DIR is not empty and has no resources/app.asar, so it isn't" >&2
        echo "       a previous Collimate install. Refusing to delete it — pass a" >&2
        echo "       different --install-dir." >&2
        exit 2
    fi

    if [ "$RESET" = "1" ]; then
        uninstall_app
    fi
    rm -rf "$APP_DIR"
    mkdir -p "$APP_DIR"
    # `cp -a … /.` copies the contents, preserving the mode bits that make
    # the Electron binary and chrome-sandbox executable.
    cp -a "$UNPACKED/." "$APP_DIR/"
    echo "      app            $APP_DIR"

    # The website's mark at every standard size plus the SVG (see the canonical
    # dev-install note): desktop/icons/hicolor/<size>/apps/collimate.{png,svg},
    # copied into the user's hicolor theme one file at a time so nothing
    # else in that theme is touched.
    ICONS_INSTALLED=0
    for src in "$DESKTOP_DIR"/icons/hicolor/*/apps/collimate.*; do
        [ -f "$src" ] || continue
        rel="${src#"$DESKTOP_DIR"/icons/hicolor/}"
        mkdir -p "$ICON_ROOT/$(dirname "$rel")"
        cp -f "$src" "$ICON_ROOT/$rel"
        ICONS_INSTALLED=$((ICONS_INSTALLED + 1))
    done
    [ "$ICONS_INSTALLED" -gt 0 ] || {
        echo "error: no icons under $DESKTOP_DIR/icons/hicolor — is the checkout complete?" >&2
        exit 1
    }
    echo "      icons          $ICON_ROOT/{16…512,scalable}/apps/collimate.{png,svg} ($ICONS_INSTALLED files)"
    # The launcher's Icon= is an ABSOLUTE path to a copy beside the binary,
    # not the theme name, and the theme set above is the complement, not the
    # primary. A theme name is resolved through Qt's/GTK's icon loader, which
    # maps the theme's icon-theme.cache ONCE per process and trusts it — so
    # a shell (DankMaterialShell, waybar, …) that was already running when
    # these files landed keeps answering "no such icon" from its stale map
    # until it restarts, and the launcher shows a placeholder letter. An
    # absolute path skips the loader entirely and is honoured by every
    # launcher that follows the desktop-entry spec.
    #
    # The copy is named by its content (collimate-<12 hex of the PNG's
    # sha256>.{png,svg}), so a new mark is a new path. KDE Plasma caches
    # icon pixmaps by path (~/.cache/icon-cache.kcache and plasmashell's own
    # pixmap cache), and a launcher or pinned task whose Icon= path is
    # unchanged keeps the old picture however the file behind it changed.
    # The rm -rf of APP_DIR above already dropped the previous build's
    # names, and --uninstall takes these with the tree.
    ICON_HASH="$(sha256sum < "$DESKTOP_DIR/icons/app-icon.png")"
    LAUNCHER_ICON="$APP_DIR/collimate-${ICON_HASH:0:12}.png"
    cp -f "$DESKTOP_DIR/icons/app-icon.png" "$LAUNCHER_ICON"
    cp -f "$DESKTOP_DIR/icons/app-icon.svg" "${LAUNCHER_ICON%.png}.svg"
    echo "      icon           $LAUNCHER_ICON"

    # The launcher. Its *filename* is load-bearing on Wayland: index.ts calls
    # app.setDesktopName('collimate.desktop'), which becomes the surface's
    # app_id, and a compositor matches the icon and any window rules by
    # finding a collimate.desktop with that id. Under X11 the same job is
    # done by StartupWMClass, which has to match Chromium's WM_CLASS —
    # derived from the app name, so "Collimate" rather than the executable.
    #
    # Exec is quoted only when the path needs it (spaces or the spec's
    # reserved characters): a launcher with a lax Exec parser hands a quoted
    # plain path to exec() with the quotes still on it, and the click does
    # nothing. Quickshell/DMS, GTK and KDE all parse quotes correctly; a
    # minimal one might not, and an unquoted plain path is right everywhere.
    EXEC_BIN="$APP_DIR/$APP_BIN_NAME"
    if [[ "$EXEC_BIN" =~ [^A-Za-z0-9_./+,@:=-] ]]; then
        # Anything outside the plain set gets the spec's double quotes, with
        # the four characters that must be backslash-escaped inside them.
        EXEC_BIN="$(printf '%s' "$EXEC_BIN" | sed 's/[\\"`$]/\\&/g')"
        EXEC_BIN="\"$EXEC_BIN\""
    fi
    mkdir -p "$APPS_DIR"
    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Collimate
Comment=Local workbench for LLM generators and renderers
Exec=$EXEC_BIN %U
Icon=$LAUNCHER_ICON
Terminal=false
Categories=Development;
StartupWMClass=Collimate
StartupNotify=true
EOF
    echo "      launcher       $DESKTOP_FILE"

    # The CLI handle. A symlink so `git pull` + a rebuild is enough; only a
    # symlink, because overwriting whatever else is called `collimate` on
    # someone's PATH is not this script's business.
    BIN_DIR="$(dirname "$BIN_LINK")"
    mkdir -p "$BIN_DIR"
    if [ -L "$BIN_LINK" ] || [ ! -e "$BIN_LINK" ]; then
        ln -sfn "$APP_DIR/$APP_BIN_NAME" "$BIN_LINK"
        echo "      command        $BIN_LINK"
        case ":$PATH:" in
            *":$BIN_DIR:"*) ;;
            *) echo "      note: $BIN_DIR is not on your PATH — add it to run 'collimate'" ;;
        esac
    else
        echo "      warning: $BIN_LINK exists and is not a symlink — left alone" >&2
    fi

    refresh_desktop_caches

    # Chromium's sandbox (see the canonical dev-install note): the
    # profile is written beside the app on every install and loaded — with
    # sudo, asking first — only on a system that restricts unprivileged user
    # namespaces, where the app would otherwise abort at startup.
    write_apparmor_profile "$APP_DIR/apparmor/collimate" "$APP_DIR/$APP_BIN_NAME"
    if userns_restricted; then
        apparmor_apply "$APP_DIR/apparmor/collimate"
    fi

    write_server_spec "$USER_DATA"

    print_installed_banner 'Launch it from your app menu ("Collimate"), or run: collimate'
    ;;

# ---------------------------------------------------------------------------
Darwin)
    APP_SRC=""
    for candidate in "$RELEASE_DIR"/mac*/Collimate.app; do
        if [ -d "$candidate" ]; then
            APP_SRC="$candidate"
            break
        fi
    done
    [ -n "$APP_SRC" ] || {
        echo "error: no Collimate.app in $RELEASE_DIR — did electron-builder target mac?" >&2
        exit 1
    }

    # Packaging must produce the runnable Electron app before reset uninstalls.
    # productName is Collimate in desktop/electron-builder.yml.
    if [ ! -x "$APP_SRC/Contents/MacOS/Collimate" ] \
       || [ ! -f "$APP_SRC/Contents/Resources/app.asar" ]; then
        echo "error: incomplete Collimate.app in $APP_SRC — executable or app.asar missing" >&2
        exit 1
    fi

    APPS_PARENT="${INSTALL_DIR:-$HOME/Applications}"
    APP_DEST="$APPS_PARENT/Collimate.app"
    # ~/Applications, not /Applications: no sudo, no authorization prompt,
    # and nothing shared with other accounts on the machine — an install of
    # a local build should be as reversible as deleting a folder.
    if [ "$RESET" = "1" ]; then
        uninstall_app
    fi
    mkdir -p "$APPS_PARENT"
    rm -rf "$APP_DEST"
    cp -R "$APP_SRC" "$APP_DEST"
    echo "      app            $APP_DEST"

    write_server_spec "$USER_DATA"

    print_installed_banner 'Launch it from ~/Applications (Spotlight finds it too), or run:' \
        "  open -a \"$APP_DEST\""
    ;;

# ---------------------------------------------------------------------------
*)
    # Windows (and anything else): no install automation. The NSIS installer
    # already does this job properly — Start-menu and desktop shortcuts, an
    # uninstaller, the registry entries — and the shell is connect-only on
    # Windows anyway, so there is no serverSpec to write: it doesn't spawn a
    # server, it points at one you are running yourself.
    echo "      Automated install isn't supported on $(uname -s)."
    echo "      Run the installer in desktop/release/ (build with --win for NSIS)."
    echo "      On Windows the shell is connect-only: start the server yourself"
    echo "      and give the app its URL (README.md, 'Desktop app')."
    ;;
esac

# ---------------------------------------------------------------------------
# 5. --reset: the app now serves this build; bring every channel's capability
#    cells that came from the bundled content onto it too. build/reset_defaults.py,
#    a developer tool with no product guarantees, does the work (its docstring
#    says how) in the workspace environment, against the installed app's data
#    dir. The null keyring backend keeps a headless run from wedging on the
#    system keyring; the reset never needs a key.
# ---------------------------------------------------------------------------
if [ "$RESET" = "1" ]; then
    echo ""
    echo "[reset] Resetting every channel's capability cells to this build's content..."
    (
        cd "$ROOT_DIR"
        PYTHONPATH="$API_DIR" PYTHON_KEYRING_BACKEND="${PYTHON_KEYRING_BACKEND:-keyring.backends.null.Keyring}" \
            uv run python build/reset_defaults.py --data-dir "$USER_DATA"
    )
    echo ""
    echo "=================================="
    echo "  Reset complete."
    echo ""
    echo "  Every capability cell in $USER_DATA that admission recorded as one of"
    echo "  the content's packages holds this build's copy (a new version — its"
    echo "  history is intact); every other cell is as it was. A new channel seeds"
    echo "  the same set."
    echo "  Launch: collimate"
    echo "=================================="
fi
