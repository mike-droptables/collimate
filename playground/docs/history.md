# Playground history

Moved here on 2026-09-23 from the app's planning docs (`docs/working/`) when the browser demo was extracted into `playground/`; the paragraphs below record the decisions as they were written, not the current design (see `../README.md` for that).

## From `desktop-first.md` §D — Playground (separable decision), 2026-07-27

Untouched by A–C and decided independently: keep as the marketing demo, or
delete (`src/collimate/wasm*.py`, `api/process_manager/wasm_runner.py`,
`build/playground/`, `build/assemble_site.sh`, SDK `demo/` +
`wasm.collgen/.collren`, `useIsWasm` + polling fallback + `IS_WASM`
branches). The playground's web generator was already absent (wasm never
ran it), so A changed nothing in its behavior.

The tiers table of that program listed the playground as a third tier:

| Tier | How you get it | What works |
| --- | --- | --- |
| **Playground** | collimate.dev | wasm subset, unchanged by this program |

## From `electron-only-delta.md` — the deletion row

| What | Where | Why it existed |
| --- | --- | --- |
| Pyodide playground: wasm runtime, boot, service-worker ASGI bridge, site assembly, demo tree, wasm bundles, frontend wasm branches | `src/collimate/wasm*.py`, `api/process_manager/wasm_runner.py`, `build/playground/` (`boot.js`, `sw-gate.js`), `build/assemble_site.sh`, SDK `demo/`, `wasm.collgen/.collren`, `useIsWasm`, SSE→polling fallback in `useChannelSSE`, `IS_WASM`/`WasmNotSupportedError` plumbing | "Try it in the browser" story; replaced by "download the app" |

The delta's execution order placed the playground fourth: "(1) cdp/ + web
generator modes, (2) session proxy + registry, (3) port proxy + pointer field,
(4) playground, (5) PWA, (6) capability plumbing, (7) launcher". Its "Redone"
list noted that `getApiUrl()`'s `'../api'` relative fallback existed for GitHub
Pages, and that the landing page would become "essay + download links; no
playground".

## From `dev-plan.md`, 2026-07-28

The PWA-removal workstream carried the caveat: "the GitHub Pages playground has
its own Pyodide `/sw.js` (`build/playground/sw.js`, `sw-gate.js`) — untouched",
and when W1 landed the best-effort SW unregister in `main.tsx` left "Pages'
Pyodide worker untouched".

## From `human-tasks.md` §S5 — GitHub Pages

Public `collimate` → Settings → Pages → Source: **GitHub Actions**. The Pages
workflow fires on the next sync push. (Now `.github/workflows/playground.yml`.)

## Resolution

Decision D was taken on 2026-09-23: neither keep-as-is nor delete. The
playground moved out of the main code entirely into `playground/`, which
builds the demo by staging and patching the app rather than by branches inside
it. Everything listed in the rows above was deleted from `api/`, `frontend/`,
`build/` and the SDK.
