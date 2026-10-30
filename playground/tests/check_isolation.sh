#!/usr/bin/env bash
# Isolation guard: the browser demo lives ONLY under playground/. Nothing
# about it — the tokens below — may appear in the main tree, except the
# integration points and legitimate unrelated uses listed further down.
#
# Scans every tracked and untracked-but-not-ignored file (so a new file that
# has not been `git add`ed is checked too) plus AGENTS.md, which is
# gitignored in this repo but carries the rule. Runs in CI from the repo root.
set -euo pipefail

cd "$(dirname "$0")/../.."

# The demo's own names: its Python runtime and package installer, its guest
# engine binary (quickjs.wasm) and the worker that hosts it, the JSPI
# capability its verifier needs, an is-wasm branch flag (the shape the old
# in-tree demo took), its service-worker gate, its boot script and the old
# build's names. Not the bare technology: the app itself ships wasm
# (esbuild-wasm, pdfium), and that is no mention of the demo.
TOKENS='playground|pyodide|micropip|emscripten|quickjs\.wasm|verifier-worker\.js|JSPI|is_?wasm|COLLIMATE_RUNTIME|IDBFS|sw-gate|boot\.js|assemble_site|gh-pages'

# Exact file:pattern pairs that may mention a token. Anything else is a leak.
allowed() {
    local file="$1" line="$2"
    case "$file" in
        README.md)
            [[ "$line" == '| `playground/` | Self-contained browser demo build. Everything about it lives in that folder. |' ]] ;;
        AGENTS.md)
            [[ "$line" == *'`playground/` is the only place the browser demo (pyodide) exists.'* ]] ;;
        .github/workflows/playground.yml) true ;;
        # Next-milestone plans discuss the browser demo overlay as a project of
        # their own (how it derives facts from main code); plans are not code.
        docs/plans/2026-10-25-next-milestone/*) true ;;
        .github/workflows/test.yml)
            [[ "$line" == *'run: bash playground/tests/check_isolation.sh'* ]] ;;
        # Exact owner-supplied provenance is preserved unchanged. These three
        # dated lines authorize the work; no main-tree runtime mention is added.
        docs/plan0928/owner-decisions-2026-10-05.md)
            case "$line" in
                '8/8 and Playground [37313366441](https://github.com/mike-droptables/collimatedev/actions/runs/37313366441)') true ;;
                '| OW-P4-19 | **Classification ratified.** Under section 1 of the 2026-10-04 decisions, verdict shapes, identity, approvals and typed-write rules are the interface and were untouched; a backend mechanism and performance bounds are not. The confined guest runtime was within the standing authorization. Separately: a question that has been reported to the owner is closed only by the owner. See 6.1. Substance: the product deadline stays; the auxiliary (Playground) runtime discloses its limits the way the sealed preview does; a contention refusal reads as "verifier busy, try again", never as an invalid package; the E2E logs the refusal reason so a recurrence of the 45-minute cancellation is read from the log, not reproduced. | P4R02, 6.1 |') true ;;
                '| P4R02 | P4R | app | The auxiliary runtime states its limits and refuses legibly | The Playground build discloses its verifier limits (heap, stack, tested engine, startup refusal elsewhere) in its own chrome or startup page, as the sealed preview discloses network reach. A deadline refusal under contention reads "verifier busy, try again" and is never labelled an invalid package. The E2E records the refusal reason and timing on every refusal. Product deadlines unchanged. OW-P4-19. |') true ;;
                *) false ;;
            esac ;;
        # Exact owner-supplied hosted-deferral provenance; no broader allowance.
        docs/plan0928/owner-decisions-2026-10-06.md)
            [[ "$line" == 'the deferral: `Test`, `Playground` (auxiliary), the native candidate and' ]] ;;
        # Third-party emscripten builds: the lockfile's pyodide-targeted wheels
        # (*-pyemscripten_*_wasm32.whl) and platform markers, and the emscripten
        # glue inside pinned snapshot modules; the bodies themselves live on
        # the module-snapshot branch and are git-ignored on main, so only the
        # lockfile's wheel markers remain in the scanned tree.
        uv.lock)
            [[ "$line" == *-pyemscripten_*_wasm32.whl* || "$line" == *"marker = \"sys_platform "*" 'emscripten'\""* ]] ;;
        # The 2026-10-11 plan, its frozen code maps and its phase records name
        # the demo's overlay anchors and this guard as plan text and recorded
        # commands; none of it is product code or product documentation.
        docs/plan1011/*) true ;;
        *) false ;;
    esac
}

leaks=0
while IFS= read -r hit; do
    file="${hit%%:*}"
    rest="${hit#*:}"
    line="${rest#*:}"
    if ! allowed "$file" "$line"; then
        echo "LEAK: $hit"
        leaks=$((leaks + 1))
    fi
done < <(
    { git ls-files -z --cached --others --exclude-standard | grep -zvE '^playground/'; printf 'AGENTS.md\0'; } \
        | xargs -0 grep -InE -i "$TOKENS" -- 2>/dev/null || true
)

if [ "$leaks" -ne 0 ]; then
    echo "check_isolation: $leaks line(s) mention the browser demo outside playground/" >&2
    exit 1
fi
echo "check_isolation: clean"
