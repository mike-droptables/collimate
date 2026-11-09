#!/usr/bin/env bash
# ci_local.sh — the local pre-publish gate: run, on this linux machine and in
# this checkout, the commands the public repo's workflows run on a push and on
# a release tag, one named step at a time, before
# `python3 build/push_public.py --push`.
#
# Usage: bash build/ci_local.sh [--fast] [--serial] [--only STEP[,STEP...]]... [--installers] [--list]
#   --fast          skip the three slow steps (desktop-e2e, site-e2e,
#                   desktop-package); they are reported as skipped
#   --serial        run api-tests in its place in the chain instead of in
#                   the background beside it
#   --only STEPS    run only these steps (comma-separated, repeatable), in
#                   the order below; the setup step is not implied
#   --installers    desktop-package builds the installers (AppImage, deb)
#                   and drives the AppImage, as release.yml does, instead of
#                   `--dir` and the unpacked app; the AppImage is copied to
#                   tmp/ci-local/appimage/, never ~/CollimateApp
#   --list          print the steps and exit
#
# Each step runs in its own bash process under the job's timeout, with its
# whole output in tmp/ci-local/<step>.log. A failing step never stops the
# run: every step runs. The machine is shared, so the steps run one at a
# time, with one exception: unless --serial, api-tests (pytest -n 4, the
# worker count of GitHub's ubuntu-24.04 runner, CI_LOCAL_WORKERS overrides
# it; no browser) starts in the background after the lint steps and the
# chain goes on beside it, its browser suites still one at a time; the
# steps that write api/collimate_server/static or dist/desktop/server
# (native-wheel, desktop-e2e, desktop-package) wait for it first, because
# api/tests branches on those. The run ends with a table of step, result (pass, FAIL,
# skipped), duration and log, also written to tmp/ci-local/summary.txt, and
# the last lines of each failing log. Exit 1 when any step failed.
#
# Environment, as GitHub's runners have it: CI=true, PYTHON_KEYRING_BACKEND
# =keyring.backends.null.Keyring, python3 and python resolving to CPython
# 3.13 (setup-python; a shim in tmp/ci-local/bin found with `uv python find
# --system 3.13`, never a project venv),ELECTRON_RUN_AS_NODE unset (agent shells export it, and it turns
# Electron into node). Electron runs under `xvfb-run -a` when it is
# installed, as the workflows do; otherwise on $DISPLAY with WAYLAND_DISPLAY
# dropped, so windows appear on that display while the suite runs.
#
# The steps, and where each comes from:
#   setup               setup-toolchain (.github/actions): uv sync --locked
#                       --all-groups (stricter than CI's unlocked sync: a
#                       stale uv.lock fails here), build/prepare_sdk_checkout.sh
#                       (its npm packages, built outputs, payloads, the venv,
#                       the module snapshot bodies); `npm ci` in frontend/,
#                       desktop/ and playground/ only where the lockfile or
#                       Node changed since the last install
#                       (build/npm_ci_if_stale.sh: CI's `npm ci` on a fresh
#                       runner), the pinned Electron binary, Playwright's
#                       Chromium for frontend/ and playground/
#   generate-check      test.yml api-tests / sdk-ci.yml test: scripts/generate.py --check
#   workflows-lint      test.yml workflows-lint: actionlint 1.7.12 in docker
#   api-lint            test.yml api-lint: ruff check api/, the isolation guard
#   api-tests           test.yml api-tests: pytest api/tests -n 4 -v
#   api-git-versions    test.yml api-git-versions: the git suites with git
#                       2.31.1 and 2.47.3 from ~/git-<version> (the job's own
#                       prefix); skipped when neither is built there
#   api-typecheck       test.yml api-typecheck: pyright
#   sdk-tests           sdk-ci.yml test: ruff check sdk, pytest sdk/tests
#   sdk-source-312      sdk-ci.yml source-312: sdk/tests on 3.12 against sdk/src
#   frontend-typecheck  test.yml frontend-typecheck: typecheck, lint
#   frontend-unit       test.yml frontend-unit: vitest with coverage, build,
#                       the bundle-size gate (scripts/check-bundle-size.mjs)
#   frontend-browser    test.yml frontend-browser: browser-mode vitest, build,
#                       the offline fonts spec
#   desktop-build       test.yml desktop-build: build, typecheck, vitest in desktop/
#   site-demo-unit      playground.yml demo-unit: pytest playground/tests/demo
#   site-build          playground.yml build: playground/build.sh, the isolation guard
#   site-e2e            playground.yml e2e: Playwright against playground/dist
#   native-wheel        native-wheels.yml prepare (linux-x64): the wheel with
#                       the UI, check_native_wheels.py, release_policy.py check-wheel
#   desktop-e2e         test.yml desktop-e2e: the UI copied into
#                       api/collimate_server/static, the Electron Playwright suite
#   desktop-package     release.yml desktop (linux-x64): build/build_desktop.sh
#                       --dir, build/check_server_env.py on its
#                       resources/server --offline, then packaged.spec.ts with
#                       COLLIMATE_E2E_PACKAGED (the unpacked app; the AppImage
#                       with --installers)
#
# NOT COVERED, on purpose or because this machine cannot:
#   - The mac and Windows legs: native-wheels.yml's darwin wheels and their
#     install legs, release.yml's darwin-arm64, darwin-x64 and win-x64
#     packages, mac signing, notarization and the signature checks. Those run
#     only on GitHub's runners: release.yml runs its build jobs as a
#     rehearsal on every push to the public main (`build/push_public.py
#     --push` without `--tag`) and on a workflow_dispatch (its `tag` input),
#     which is how they are exercised before a tag.
#   - linux-arm64 (another architecture's runner).
#   - native-wheels.yml's install legs (pip and uvx installs of the wheel with
#     compilers and Node denied, the verifier proof, a server to /ready) and
#     its distribution job (the sdist, and a wheel rebuilt from it offline):
#     their Python is inline in the workflow. desktop-package freezes and
#     boots the same wheel, which covers the startup, not the installers.
#   - Publishing and everything after it: the PyPI upload, the GitHub release,
#     its asset set and feed, release-guard.yml, the Pages deploy.
#   - GitHub-only mechanics: caches, artifact uploads and downloads, the
#     opt-in visual-regression job, the Electron trace-proof dispatch,
#     `npx playwright install-deps` and the AppArmor sysctl (both need root:
#     on this machine unprivileged user namespaces already work, which is all
#     the sysctl is for).
#   - The public tree. This runs the checkout it lives in, docs/ and the
#     other excluded paths present; GitHub runs the push tool's tree without
#     them. To run the public tree: after a successful dry run of
#     build/push_public.py, `git clone --branch publish <scratch>/public.git
#     <dir>` and `bash <dir>/build/ci_local.sh`.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELF="$ROOT/build/ci_local.sh"
LOG_DIR="$ROOT/tmp/ci-local"
SKIP_EXIT=77

STEPS=(setup generate-check workflows-lint api-lint api-tests api-git-versions api-typecheck
       sdk-tests sdk-source-312 frontend-typecheck frontend-unit frontend-browser desktop-build
       site-demo-unit site-build site-e2e native-wheel desktop-e2e desktop-package)
SLOW=(desktop-e2e site-e2e desktop-package)
# Unless --serial, api-tests (no browser, pytest's four workers) runs in the
# background beside the chain, whose steps use one browser or none, one at
# a time. The steps that write what api/tests looks at
# (api/collimate_server/static, dist/desktop/server: several tests branch on
# whether they exist, and GitHub's api-tests job has neither) wait for it.
BACKGROUND=api-tests
AFTER_BACKGROUND=(native-wheel desktop-e2e desktop-package)

# Each step's limit, in minutes: its job's timeout-minutes, or 30 where the
# job sets none (GitHub's default is 360, which is no use as a hang guard).
limit_for() {
    case "$1" in
        setup) echo 30 ;;
        generate-check|workflows-lint|api-lint) echo 10 ;;
        api-tests|site-e2e) echo 45 ;;
        api-git-versions|desktop-e2e) echo 30 ;;
        api-typecheck|sdk-tests) echo 20 ;;
        sdk-source-312) echo 25 ;;
        frontend-browser|desktop-build) echo 15 ;;
        desktop-package) echo 60 ;;
        *) echo 30 ;;
    esac
}

# --------------------------------------------------------------------------
# Helpers the steps share. Every step runs with `set -euo pipefail` from
# the repository root.
# --------------------------------------------------------------------------
skip() {
    printf '%s\n' "$*" > "$LOG_DIR/$CI_LOCAL_STEP.skip"
    echo "SKIPPED: $*"
    exit "$SKIP_EXIT"
}

run() {
    echo ""
    echo "+ $*"
    "$@"
}

# The UI, built once per run: frontend-unit builds it, and a later step that
# needs frontend/dist reuses that build unless it is missing.
ensure_frontend_dist() {
    if [ "${CI_LOCAL_FRONTEND_FRESH:-}" = 1 ] && [ -f "$ROOT/frontend/dist/index.html" ]; then
        echo "frontend/dist: this run's build, reused"
    else
        (cd "$ROOT/frontend" && run npm run build)
    fi
}

# The Electron launcher: xvfb-run as the workflows use it, else the display
# this session has. ELECTRON_RUN_AS_NODE and WAYLAND_DISPLAY never reach it.
electron_wrap() {
    if command -v xvfb-run >/dev/null 2>&1; then
        WRAP=(env -u WAYLAND_DISPLAY xvfb-run -a)
        echo "display: xvfb-run -a"
    elif [ -n "${DISPLAY:-}" ]; then
        WRAP=(env -u WAYLAND_DISPLAY)
        echo "display: no xvfb-run on this machine; using DISPLAY=$DISPLAY"
    else
        echo "environment gap: no xvfb-run and no DISPLAY; Electron needs a display" >&2
        exit 1
    fi
}

free_port() {
    python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

# --------------------------------------------------------------------------
# The steps.
# --------------------------------------------------------------------------
step_setup() {
    local node_version
    node_version="$(node --version)"
    echo "node $node_version, $(uv --version), python3 -> $(python3 --version)"
    case "$node_version" in
        v24.*) [ "$(echo "$node_version" | cut -d. -f2)" -ge 15 ] || { echo "the workflows use Node >=24.15.0 <25" >&2; exit 1; } ;;
        *) echo "the workflows use Node >=24.15.0 <25, this is $node_version" >&2; exit 1 ;;
    esac
    run uv sync --locked --all-groups
    run bash build/prepare_sdk_checkout.sh
    for package in frontend desktop playground; do
        run bash build/npm_ci_if_stale.sh "$package"
    done
    (cd desktop && run node node_modules/electron/install.js)
    (cd frontend && run npx playwright install chromium)
    (cd playground && run npx playwright install chromium)
}

step_generate_check() {
    run python3 scripts/generate.py --check
}

step_workflows_lint() {
    command -v docker >/dev/null 2>&1 || { echo "environment gap: docker is not installed" >&2; exit 1; }
    run docker run --rm -v "$ROOT:/repo" -w /repo rhysd/actionlint:1.7.12 -color
}

step_api_lint() {
    run uv run --locked ruff check api/
    run bash playground/tests/check_isolation.sh
}

step_api_tests() {
    # The tree as the job sees it: no built UI in the server's static folder
    # and no server environment (an earlier run's desktop steps leave both;
    # tests branch on them). desktop-package checks the environment itself.
    run rm -rf api/collimate_server/static
    export COLLIMATE_SERVER_ENV="$LOG_DIR/no-server-environment"
    run uv sync --all-groups
    run uv run pytest api/tests -n "${CI_LOCAL_WORKERS:-4}" -v
}

step_api_git_versions() {
    local ran=0 version prefix
    for version in 2.31.1 2.47.3; do
        prefix="$HOME/git-$version"
        if [ ! -x "$prefix/bin/git" ]; then
            echo "git $version: not built at $prefix (the job builds it from its release tarball:"
            echo "  make prefix=$prefix NO_EXPAT=1 NO_GETTEXT=1 NO_PERL=1 NO_PYTHON=1 NO_TCLTK=1 install)"
            continue
        fi
        ran=1
        (
            export PATH="$prefix/bin:$PATH"
            test "$(git --version)" = "git version $version"
            test -x "$(git --exec-path)/git-remote-https"
            test -x "$(git --exec-path)/git-http-backend"
            run uv run pytest -n "${CI_LOCAL_WORKERS:-4}" -q \
                api/tests/test_pytest_git_*.py api/tests/test_pytest_repository_*.py
            run uv run pytest -q sdk/tests/test_git_*.py sdk/tests/test_env_git_*.py
        )
    done
    [ "$ran" = 1 ] || skip "no git 2.31.1 or 2.47.3 under ~/git-<version>; api-tests ran the git suites with $(git --version)"
}

step_api_typecheck() {
    run uv run --locked pyright
}

step_sdk_tests() {
    run uv run ruff check sdk
    run uv run pytest sdk/tests -q
}

step_sdk_source_312() {
    local requirements
    # Global, not local: the EXIT trap runs after the function has returned.
    temp="$(mktemp -d "${TMPDIR:-/tmp}/collimate-ci-local-312.XXXXXX")"
    trap 'rm -rf "$temp"' EXIT
    run uv venv --no-project --python 3.12 "$temp/venv312"
    read -r -a requirements <<< "$(PYTHONPATH=sdk/src python3 -c 'from collimate.shared import SDK_REQUIREMENTS; print(*SDK_REQUIREMENTS)')"
    run uv pip install --python "$temp/venv312/bin/python" --group pyproject.toml:dev "${requirements[@]}"
    cd "$temp"
    PYTHONPATH="$ROOT/sdk/src" run "$temp/venv312/bin/python" -c 'import collimate, gen, importlib.util, sys; from pathlib import Path; src = Path(sys.argv[1]).resolve(); paths = [Path(module.__file__).resolve() for module in (collimate, gen)]; assert sys.version_info[:2] == (3, 12), sys.version; assert all(path.is_relative_to(src) for path in paths), paths; assert importlib.util.find_spec("collimate_server") is None; print(*paths)' "$ROOT/sdk/src"
    PYTHONPATH="$ROOT/sdk/src" run "$temp/venv312/bin/python" -m pytest --rootdir "$ROOT" -c "$ROOT/pyproject.toml" -p no:cacheprovider -q "$ROOT/sdk/tests"
}

step_frontend_typecheck() {
    cd frontend
    run npm run typecheck
    run npm run lint
}

step_frontend_unit() {
    cd frontend
    run npm run test:coverage
    run npm run build
    run npm run bundle:check
}

step_frontend_browser() {
    cd frontend
    # Browser-mode vitest watches files even for one run; polling keeps it
    # alive when the machine's inotify watches are spent.
    CHOKIDAR_USEPOLLING=1 run npm run test:browser
    ensure_frontend_dist
    run npx playwright test -c playwright.fonts.config.ts
}

step_native_wheel() {
    local out="$LOG_DIR/wheel" builder="$LOG_DIR/venv-build"
    ensure_frontend_dist
    run rm -rf api/collimate_server/static
    run cp -r frontend/dist api/collimate_server/static
    run node sdk/scripts/build_renderer_runtime.mjs
    run python3 sdk/scripts/prepare_renderer_compilers.py
    # The job's sequence for TARGET=linux-x64: its own target, then both
    # linux payloads, then the check.
    run python3 sdk/scripts/prepare_verifier_engine.py --target linux-x64
    run python3 sdk/scripts/prepare_verifier_engine.py --target linux-x64
    run python3 sdk/scripts/prepare_verifier_engine.py --target linux-arm64
    run python3 sdk/scripts/prepare_verifier_engine.py --verify-only
    [ -x "$builder/bin/python" ] || run uv venv --python 3.13 "$builder"
    run uv pip install --python "$builder/bin/python" build
    run rm -rf "$out"
    run "$builder/bin/python" -m build --wheel --outdir "$out"
    run python3 sdk/scripts/check_native_wheels.py "$out"
    run python3 build/release_policy.py check-wheel --wheel "$out"/collimate-*.whl
}

step_desktop_build() {
    cd desktop
    run npm run build
    run npm run typecheck
    run npm test
}

step_desktop_e2e() {
    local WRAP=()
    electron_wrap
    run uv sync --all-groups
    (cd desktop && run node node_modules/electron/install.js)
    ensure_frontend_dist
    run rm -rf api/collimate_server/static
    run cp -r frontend/dist api/collimate_server/static
    cd frontend
    run "${WRAP[@]}" npx playwright test -c playwright.electron.config.ts
}

step_site_demo_unit() {
    run uv sync --all-groups
    run .venv/bin/python -m pytest playground/tests/demo -q
}

step_site_build() {
    run bash playground/build.sh
    run bash playground/tests/check_isolation.sh
}

step_site_e2e() {
    [ -d playground/dist ] || {
        echo "playground/dist is missing: the site-build step makes it (the e2e job downloads that job's output)" >&2
        exit 1
    }
    cd playground
    # A port of its own: another checkout on this machine may hold the
    # default 4321, and CI mode never reuses a running server.
    COLLIMATE_E2E_PORT="$(free_port)" run npx playwright test
}

step_desktop_package() {
    local WRAP=() server executable
    electron_wrap
    if [ "${CI_LOCAL_INSTALLERS:-}" = 1 ]; then
        PYTHON=python3 run bash build/build_desktop.sh --appimage-dir "$LOG_DIR/appimage"
    else
        PYTHON=python3 run bash build/build_desktop.sh --dir
    fi
    server="desktop/release/linux-unpacked/resources/server"
    # Exit 3 from the check is no network namespace: an environment gap
    # (release.yml lifts AppArmor's restriction with a root sysctl first).
    run python3 build/check_server_env.py "$server" --offline
    if [ "${CI_LOCAL_INSTALLERS:-}" = 1 ]; then
        shopt -s nullglob
        local images=(desktop/release/*.AppImage)
        shopt -u nullglob
        [ "${#images[@]}" -eq 1 ] || { echo "expected one AppImage in desktop/release, found ${#images[@]}" >&2; exit 1; }
        executable="$ROOT/${images[0]}"
        export APPIMAGE_EXTRACT_AND_RUN=1
    else
        executable="$ROOT/desktop/release/linux-unpacked/collimate-desktop"
    fi
    cd frontend
    COLLIMATE_E2E_PACKAGED="$executable" run "${WRAP[@]}" npx playwright test -c playwright.electron.config.ts packaged.spec.ts
}

# --------------------------------------------------------------------------
# One step, in its own process: `ci_local.sh __step <name>`.
# --------------------------------------------------------------------------
if [ "${1:-}" = __step ]; then
    set -euo pipefail
    export CI_LOCAL_STEP="$2"
    cd "$ROOT"
    "step_${2//-/_}"
    exit 0
fi

# --------------------------------------------------------------------------
# The run.
# --------------------------------------------------------------------------
FAST=
SERIAL=
ONLY=()
while [ $# -gt 0 ]; do
    case "$1" in
        --fast) FAST=1 ;;
        --serial) SERIAL=1 ;;
        --installers) export CI_LOCAL_INSTALLERS=1 ;;
        --only)
            shift
            [ -n "${1:-}" ] || { echo "error: --only needs a step name" >&2; exit 2; }
            IFS=, read -r -a names <<< "$1"
            ONLY+=("${names[@]}")
            ;;
        --only=*) IFS=, read -r -a names <<< "${1#--only=}"; ONLY+=("${names[@]}") ;;
        --list) printf '%s\n' "${STEPS[@]}"; exit 0 ;;
        -h|--help) sed -n '2,/^set -uo pipefail/p' "$SELF" | sed '$d'; exit 0 ;;
        *) echo "error: unknown argument: $1 (bash build/ci_local.sh --help)" >&2; exit 2 ;;
    esac
    shift
done
for name in "${ONLY[@]}"; do
    [[ " ${STEPS[*]} " == *" $name "* ]] || { echo "error: no step named '$name' (--list names them)" >&2; exit 2; }
done

mkdir -p "$LOG_DIR/bin"
exec 9> "$LOG_DIR/.lock"
flock -n 9 || { echo "error: another ci_local.sh run holds $LOG_DIR/.lock" >&2; exit 2; }

# GitHub's runner environment.
export CI=true
export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring
unset ELECTRON_RUN_AS_NODE
python313="$(uv python find --system 3.13 2>/dev/null)" || { echo "error: no CPython 3.13 (uv python install 3.13)" >&2; exit 2; }
ln -sf "$python313" "$LOG_DIR/bin/python3"
ln -sf "$python313" "$LOG_DIR/bin/python"
export PATH="$LOG_DIR/bin:$PATH"

selected() {
    [ "${#ONLY[@]}" -eq 0 ] && return 0
    [[ " ${ONLY[*]} " == *" $1 "* ]]
}

commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
changes="$(git -C "$ROOT" status --porcelain 2>/dev/null | wc -l)"
echo "ci_local: $ROOT at $commit ($changes uncommitted paths), logs in $LOG_DIR"

declare -A RESULT DURATION

# One step, start to end: its result and seconds go to <step>.status, so a
# step run in the background reports the same way as one in the foreground.
execute() {
    local step="$1" log="$LOG_DIR/$1.log" started=$SECONDS status result
    rm -f "$LOG_DIR/$step.skip" "$LOG_DIR/$step.status"
    timeout -k 60 "$(limit_for "$step")m" bash "$SELF" __step "$step" > "$log" 2>&1 < /dev/null 9>&-
    status=$?
    case "$status" in
        0) result=pass ;;
        "$SKIP_EXIT") result=skipped ;;
        124|137) result=FAIL; echo "TIMEOUT after $(limit_for "$step") minutes" >> "$log" ;;
        *) result=FAIL ;;
    esac
    echo "$result $((SECONDS - started))" > "$LOG_DIR/$step.status"
}

collect() {
    local step="$1" result seconds
    read -r result seconds < "$LOG_DIR/$step.status"
    RESULT[$step]=$result DURATION[$step]=$seconds
    printf '%-20s %-8s %4ss\n' "$step" "$result" "$seconds"
}

# The background step's process, collected before any step that needs it
# finished and at the end of the run.
BG_PID=
collect_background() {
    [ -n "$BG_PID" ] || return 0
    echo "(waiting for $BACKGROUND)"
    wait "$BG_PID"
    BG_PID=
    collect "$BACKGROUND"
}

started_all=$SECONDS
for step in "${STEPS[@]}"; do
    if ! selected "$step"; then
        continue
    fi
    if [ -n "$FAST" ] && [[ " ${SLOW[*]} " == *" $step "* ]]; then
        RESULT[$step]=skipped DURATION[$step]=0
        printf 'skipped (--fast)\n' > "$LOG_DIR/$step.skip"
        : > "$LOG_DIR/$step.log"
        printf '%-20s skipped (--fast)\n' "$step"
        continue
    fi
    if [[ " ${AFTER_BACKGROUND[*]} " == *" $step "* ]]; then
        collect_background
    fi
    if [ -z "$SERIAL" ] && [ "$step" = "$BACKGROUND" ]; then
        execute "$step" &
        BG_PID=$!
        printf '%-20s started in the background\n' "$step"
        continue
    fi
    printf '%-20s running...\r' "$step"
    execute "$step"
    collect "$step"
    [ "$step" = frontend-unit ] && [ "${RESULT[$step]}" = pass ] && export CI_LOCAL_FRONTEND_FRESH=1
done
collect_background

failed=0
for step in "${STEPS[@]}"; do
    [ "${RESULT[$step]:-}" = FAIL ] && failed=1
done
{
    echo ""
    echo "ci_local summary: $commit ($changes uncommitted paths), $(date -u +%Y-%m-%dT%H:%M:%SZ), $(( (SECONDS - started_all) / 60 )) min"
    printf '%-20s %-8s %8s  %s\n' STEP RESULT TIME LOG
    for step in "${STEPS[@]}"; do
        [ -n "${RESULT[$step]:-}" ] || continue
        note=""
        [ -f "$LOG_DIR/$step.skip" ] && note=" ($(head -c 120 "$LOG_DIR/$step.skip"))"
        printf '%-20s %-8s %7ss  %s%s\n' "$step" "${RESULT[$step]}" "${DURATION[$step]}" "tmp/ci-local/$step.log" "$note"
    done
} | tee "$LOG_DIR/summary.txt"

for step in "${STEPS[@]}"; do
    [ "${RESULT[$step]:-}" = FAIL ] || continue
    echo ""
    echo "---- $step: last lines of tmp/ci-local/$step.log"
    tail -n 15 "$LOG_DIR/$step.log"
done
exit "$failed"
