#!/bin/bash
# dev_desktop.sh — Build the server/UI and shell, then launch from this checkout.
# Usage: bash build/dev_desktop.sh [--skip] [--wipe]
#   --skip       reuse api/static; always rebuild the shell
#   --wipe       remove the selected profile's channels before launching
# Unknown flags are errors; stop the selected profile's server before wiping.
# Unpackaged runs default to the separate Collimate Dev profile.
# COLLIMATE_APP_PROFILE=stable|dev overrides that choice deliberately.
# Profile isolation, launcher setup and installed builds:
# docs/ai_tools/notes/2026-10-11/project_desktop_dev_install.md
set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$ROOT_DIR/api"
DESKTOP_DIR="$ROOT_DIR/desktop"

SKIP=0
WIPE=0
for arg in "$@"; do
    case "$arg" in
        --skip) SKIP=1 ;;
        --wipe) WIPE=1 ;;
        -h|--help)
            # The header block: line 2 through the last comment line before
            # `set -e`. Computed rather than a fixed range, so editing the
            # header can't silently truncate --help — the same rule, for the
            # same reason, as build/build_desktop.sh.
            sed -n '2,/^set -e/p' "${BASH_SOURCE[0]}" | sed '$d'
            exit 0
            ;;
        *)
            echo "error: unknown argument: $arg" >&2
            echo "       Usage: bash build/dev_desktop.sh [--skip] [--wipe]" >&2
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Which profile the launch below will run — resolved here because --wipe has
# to aim at that profile's user-data dir and nothing else.
#
# The grammar lives in desktop/src/main/profile.ts (resolveProfile): the value
# is trimmed and lowercased before it is compared, empty or unrecognised falls
# back to the default, and the default for an unpackaged run — which is all
# this script ever launches — is 'dev'. This mirrors that, and has to keep
# mirroring it: a COLLIMATE_APP_PROFILE=STABLE the shell honours but a
# case-sensitive test here does not would wipe the dev channels and then open
# the app on the stable ones, which is precisely the mix-up the profile split
# exists to prevent. The tr/sed pipeline rather than ${var,,} or ${var//} so
# this still runs under the bash 3.2 macOS ships.
# ---------------------------------------------------------------------------
APP_PROFILE="$(printf %s "${COLLIMATE_APP_PROFILE:-}" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
case "$APP_PROFILE" in
    stable)  APP_NAME="Collimate" ;;
    dev|"")  APP_NAME="Collimate Dev" ;;
    *)
        # Not an error: resolveProfile ignores a value it doesn't recognise
        # rather than refusing to start (a typo in a shell rc file should not
        # cost you the app), so the run silently gets the unpackaged default.
        # Say so anyway — this script is the only link in the chain with a
        # terminal attached, and a 'stabel' that quietly becomes 'dev' is
        # worth hearing about before --wipe acts on it.
        echo "[dev_desktop] warning: COLLIMATE_APP_PROFILE=\"$COLLIMATE_APP_PROFILE\" is neither 'stable' nor 'dev';" >&2
        echo "              the shell ignores it, so this run — and any --wipe — uses the dev profile." >&2
        APP_NAME="Collimate Dev"
        ;;
esac

# ---------------------------------------------------------------------------
# Is a server running out of the user-data dir --wipe is aimed at?
#
# live_server_pid is build/build_desktop.sh's, verbatim, and works on every
# OS: the shell records the server it spawned in <user-data dir>/server.json
# ({pid, port, url, startedAt, version, shellVersion, spec, dataDir},
# removed on a clean stop and kept when the app quits with the server left
# running — desktop/src/main/server.ts); a record naming a dead pid is a
# crash leftover and does not count.
#
# unrecorded_server_pid is the Linux-only catch for a server no record
# names (one started by hand with --data-dir pointing here, or one whose
# record write failed): any process whose cwd is the dir — the server
# chdirs into its data dir — and whose command line runs main.py. The dir
# is compared resolved (pwd -P), as /proc/<pid>/cwd reads, so a symlink in
# HOME or XDG_CONFIG_HOME cannot hide a server. macOS has no /proc, so there
# the record is the whole check.
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
unrecorded_server_pid() {
    local dir proc cwd
    [ -d /proc/self ] || return 1
    # No such dir, no server running out of it.
    dir="$(CDPATH='' cd -- "$1" 2>/dev/null && pwd -P)" || return 1
    for proc in /proc/[0-9]*; do
        cwd="$(readlink "$proc/cwd" 2>/dev/null || true)"
        if [ "$cwd" = "$dir" ] && tr '\0' ' ' < "$proc/cmdline" 2>/dev/null | grep -q "main.py"; then
            echo "${proc#/proc/}"
            return 0
        fi
    done
    return 1
}

# build_server.sh checks npm/node itself, but the --skip path bypasses it,
# and the shell build needs npm/npx either way. uv is what the shell will
# spawn the server with; node reads server.json for --wipe's live-server
# check, which is why this runs before the wipe.
for tool in node uv npm npx; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "error: '$tool' not found on PATH — see README.md prerequisites" >&2
        exit 1
    }
done

if [ "$WIPE" = "1" ]; then
    # The shell keeps server state inside Electron's user-data dir (index.ts:
    # dataDir = userData), so that is where .collimate/ lives — not this
    # repo. The directory is named after the profile's app
    # name, which for the usual 'dev' run is "Collimate Dev": a directory with
    # a space in it, hence the quoting on every use of it. Only .collimate/ is
    # wiped; settings.json and logs/ stay.
    #
    # APP_NAME follows COLLIMATE_APP_PROFILE (above) because this has to: with
    # the shell being forced to 'stable', a wipe aimed at the default would
    # delete the data of the profile this run is not about — and leave the
    # installed app's, which is the one being launched, untouched.
    case "$(uname -s)" in
        Darwin) USER_DATA="$HOME/Library/Application Support/$APP_NAME" ;;
        *)      USER_DATA="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_NAME" ;;
    esac
    # A server still running out of that directory would keep writing into
    # the deleted files (an unlinked app.db, ghost channel dbs) — the very
    # corruption the header warns about — so the wipe is refused
    # while one runs, before anything is touched. Refused, never killed: the
    # policy build/build_desktop.sh has, whose live_server_pid this is.
    if LIVE_PID="$(live_server_pid "$USER_DATA")"; then
        echo "error: $APP_NAME's server is running (server pid $LIVE_PID, recorded in $USER_DATA/server.json)." >&2
        echo "       --wipe deletes that app's channel data — stop the local server first:" >&2
        echo "       File → Servers → Stop, or quit the app without keeping the server running. If the app" >&2
        echo "       is not open, the server was kept running at quit (or a crash left it): open the app," >&2
        echo "       stop it, then run this again." >&2
        exit 1
    fi
    if LIVE_PID="$(unrecorded_server_pid "$USER_DATA")"; then
        echo "error: a Collimate server (pid $LIVE_PID) is running from $USER_DATA." >&2
        echo "       --wipe deletes the data it is writing — stop that server first." >&2
        exit 1
    fi
    # Printed unconditionally, even when there turns out to be nothing to
    # delete: which of the two apps' data a wipe is aimed at is the one thing
    # about this flag worth being sure of before it runs.
    echo "[dev_desktop] wiping the \"$APP_NAME\" profile's data in $USER_DATA"
    target="$USER_DATA/.collimate"
    if [ -d "$target" ]; then
        echo "[dev_desktop] wiping $target"
        rm -rf "$target"
    fi
fi

if [ "$SKIP" = "1" ]; then
    echo "[dev_desktop] --skip — serving api/static/ as it stands"
    [ -d "$API_DIR/static" ] || {
        echo "error: --skip but $API_DIR/static is missing." >&2
        echo "       Run 'bash build/dev_desktop.sh' once without --skip." >&2
        exit 1
    }
else
    bash "$ROOT_DIR/build/build_server.sh"
fi

echo ""
echo "[dev_desktop] Building the shell bundle..."
cd "$DESKTOP_DIR"
bash "$ROOT_DIR/build/npm_ci_if_stale.sh" "$DESKTOP_DIR"
npm run build          # esbuild → desktop/dist/{main.cjs,preload.cjs,splash/}

# ---------------------------------------------------------------------------
# The dev window's icon and name on Wayland come from a launcher file, so make
# sure one exists.
#
# The unpackaged shell calls app.setDesktopName('collimate-dev.desktop') (the
# dev profile — desktop/src/main/profile.ts), and that string becomes the
# toplevel's app_id. A Wayland compositor has nothing else to go on: it looks
# up the app id among the installed desktop entries and takes the window's
# icon, its display name and any window rules from the entry it finds. The
# packaged app's collimate.desktop is installed by build/build_desktop.sh, but
# a dev run installs nothing, so without this the dev window sits in the
# taskbar and the switcher under a generic placeholder icon — indistinguishable
# from anything else unrecognised. NoDisplay keeps the entry out of the app
# menu: it exists to be matched by an already-running window, not to be
# clicked. (Exec still points back at this script, with --skip so a click
# doesn't silently sit through a Vite build, for the launcher that runs it
# anyway.) Written for the dev profile unconditionally — it costs a cmp, and a
# run forced to 'stable' will claim the installed app's id and use the entry
# build_desktop.sh installed for it.
#
# macOS takes the icon from the .app bundle (and an unpackaged run from
# Electron's own), so there is nothing to write there.
# ---------------------------------------------------------------------------
write_dev_launcher() {
    local data_home="${XDG_DATA_HOME:-$HOME/.local/share}" hash icon stale sycoca exec_bin body
    local launcher_dir="$data_home/applications" icon_dir="$data_home/collimate-dev"
    local launcher="$launcher_dir/collimate-dev.desktop"
    # Icon= is an absolute path rather than a theme name, because a dev run
    # never installs anything into the user's hicolor theme (see
    # build_desktop.sh's "Icon" note for why even an installed one names a
    # path) — and a path outside the checkout, named by the PNG's content
    # (collimate-<12 hex of its sha256>.png), as build_desktop.sh names its
    # copy. KDE Plasma caches icon pixmaps by path, so a path that never
    # changes keeps showing the old mark after the file behind it does.
    hash="$(sha256sum < "$DESKTOP_DIR/icons/app-icon.png")" || return 1
    icon="$icon_dir/collimate-${hash:0:12}.png"
    # Exec is quoted only when the path needs it (spaces, or the spec's
    # reserved characters) — the same rule, for the same reason, as the
    # installed launcher build/build_desktop.sh writes: a launcher with a lax
    # Exec parser hands a quoted plain path to exec() with the quotes still
    # attached, and the entry does nothing.
    exec_bin="$ROOT_DIR/build/dev_desktop.sh"
    if [[ "$exec_bin" =~ [^A-Za-z0-9_./+,@:=-] ]]; then
        exec_bin="$(printf '%s' "$exec_bin" | sed 's/[\\"`$]/\\&/g')"
        exec_bin="\"$exec_bin\""
    fi
    body="[Desktop Entry]
Type=Application
Name=Collimate Dev
NoDisplay=true
Exec=$exec_bin --skip
Icon=$icon
Terminal=false
StartupWMClass=Collimate Dev
Categories=Development;
"
    # Rewritten only when the content actually differs, so a relaunch loop is
    # a cmp and nothing else — no mtime churn for the file watchers desktop
    # environments keep on these directories. A changed mark changes the
    # entry too (its Icon= names the hash), so the icon is copied, the
    # previous marks dropped and the caches refreshed on that same path.
    printf '%s' "$body" | cmp -s - "$launcher" 2>/dev/null && [ -f "$icon" ] && return 0
    mkdir -p "$icon_dir" "$launcher_dir" 2>/dev/null || return 1
    cp -f "$DESKTOP_DIR/icons/app-icon.png" "$icon" 2>/dev/null || return 1
    for stale in "$icon_dir"/collimate-*.png; do
        [ "$stale" = "$icon" ] || rm -f "$stale"
    done
    printf '%s' "$body" > "$launcher" 2>/dev/null || return 1
    # One line of output: a file appearing in the user's applications
    # directory should not be a thing the script does silently.
    echo "[dev_desktop] wrote $launcher (the dev window's icon and name)"
    # Best-effort, as build_desktop.sh's: the menu database, then KDE's
    # service database, which Plasma's task manager reads Icon= from.
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$launcher_dir" >/dev/null 2>&1 || true
    fi
    for sycoca in kbuildsycoca6 kbuildsycoca5; do
        command -v "$sycoca" >/dev/null 2>&1 || continue
        "$sycoca" --noincremental >/dev/null 2>&1 || true
        break
    done
}
# Never fatal: the entry is cosmetic (an icon and a name for the dev window),
# and this script runs under `set -e` — an applications dir that is
# read-only, root-owned from an earlier sudo run, or on a full quota must
# cost a note on stderr, not the launch it was about to do.
if [ "$(uname -s)" != "Darwin" ] && ! write_dev_launcher; then
    echo "[dev_desktop] note: could not write the collimate-dev.desktop launcher — the dev window keeps the generic icon" >&2
fi

echo ""
echo "[dev_desktop] Launching Electron..."
# exec, so Ctrl-C and any supervisor's signals reach Electron directly; the
# shell owns the server child and shuts it down on quit.
exec npx --no-install electron dist/main.cjs
