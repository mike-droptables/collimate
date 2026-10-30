# E2E tests

Playwright specs that drive the assembled demo site in `playground/dist/` —
the exact output `build.sh` produces and CI deploys. There is no dev-server
mode: the in-browser Python stack only exists in that output, so the specs
run against the static build served by `serve.sh`.

Every request a spec makes goes the way the app's own requests go —
`fetch('../api/…')` from the page, through the service worker into pyodide.
Nothing talks to a server process.

## Layout

    tests/e2e/
      helpers/playground.ts     boot, state, runners-panel runs, phase tracking,
                                runner-settings edits, renderer loading
      smoke.spec.ts             boot + the seeded channel + SSE + one panel run + axe
      demo-runners.spec.ts      every demo runner to completion in its renderer; two
                                runs on one cell, Stop from the cell's status pane
      guards.spec.ts            single-channel 400s, browser-demo 501s, hidden controls
      keyboard-a11y.spec.ts     the demo badge/card and the runners panel by keyboard
      renderer-matrix.spec.ts   every SDK renderer serves + loads; apiBase rewrite pin
      perf-budget.spec.ts       cold-boot time budget
      visual/                   visual-regression baselines (opt-in, VISUAL=1)

## Running locally

```bash
# 1. Build the site (reads the in-repo sdk/; needs uv, npm)
bash playground/build.sh

# 2. Install the test dependencies and Playwright's Chromium once
cd playground && npm ci && npx playwright install chromium

# 3. Run the default suite (starts serve.sh on dist/ itself, or reuses one)
npm run test:e2e

# 4. The visual suite (baselines must exist)
npm run test:visual

# 5. Refresh the visual baselines after an intentional renderer change
npm run test:visual:update
```

`COLLIMATE_E2E_PORT` overrides the static server's port (default 4321). To
point the suite at an already-running server, start `npm run serve` first;
outside CI the config reuses it.

Specs boot the runtime (a few seconds warm, up to a minute cold) and most
then watch a scripted run of 20–40 s, so the per-test timeout is 150 s. The
two open-ended demo windows — the terminal's keep-open and the chat's idle
timeout — are shortened through the runner editor API (`setRunnerSettings`)
inside the specs; the shipped defaults are untouched.

## What the demo lets a spec observe

- The channel is `Demo` with the cells `Welcome` (pinned
  `README.md`) and `Sample notes` (pinned `trip-plan.md`), and the runners
  `terminal-session`, `chat-assistant`, `modify-files`. Channel routes take
  the channel's UUID, which the boot info carries
  (`window.__COLLIMATE_PLAYGROUND__.channel.id`, `demoChannelId`).
- A run is started the way a visitor starts it: the Run shortcut (Alt+R,
  on the selected cell) opens the runners panel inside the cell, the search
  box filters, Enter runs (`runFromPanel`).
  Run status comes from `GET /channels/{channel}/runs/{id}`; the phase
  (`spinning_up` → `running` → `awaiting_input` → …) is announced only on
  the channel SSE stream, so `trackPhases` records it in-page.
- Several runs can share a cell. `GET /channels/{channel}/runs/active` rows carry
  each run's `cell_id`, `label` and `folder` (`_runs/<label>`): the run's
  live surfaces (`terminal/session.terminal`, `chat/conversation.chat` +
  `chat/main.json`) are files in that folder while it lives, and the run end sweeps them into the run's
  Logs (`runLogPaths`). A run never moves the cell's pin (the user's); it
  reveals its files as tabs instead. The cell's run light (`run-light`,
  its live-run count in `data-run-count`) opens the cell's status pane,
  (`[data-runs-pane]`), whose status view (`status-view`) has one
  `status-run-row` per run (`data-run-id`); a row opens the run's drilldown
  (`run-drilldown`), whose `run-stop` stops it.
- Renderer content lives in shadow roots (native renderers) or srcdoc
  frames (markdown through the sandbox). `getByText` pierces shadow roots. An
  unfocused cell interposes a click-to-focus overlay — `focusCell` first.
- The demo terminal's output is readable from Python
  (`terminalOutput`) — a deterministic check that typed input was echoed.

## Visual baselines

Baselines are rendered with Linux CI fonts. Seed them from a Linux runner
(the `playground` workflow's `update_visual_baselines` dispatch input
uploads them as an artifact; or a local container matching
`mcr.microsoft.com/playwright:v<version>-noble`, the version pinned in
`package.json`), commit the `.png` files under
`tests/e2e/visual/**-snapshots/`, and pull requests labelled `visual` then
gate on diffs.

The P4F46 matrix captures three static native fixtures: text, table, and
schema_form. It asserts that all three exist in the current native catalog
and that all three screenshot comparisons complete. A JSON attachment records
covered, advertised, uncovered and completed IDs. The remaining native and
iframe renderers have import/serving coverage in renderer-matrix.spec.ts;
they have no visual baseline here. Sources are canonical compiled module.js,
with source-hash identity checks, mounted in a real shadow root. Readiness and
fixture contents are required before each capture; there is no settle fallback.

Run `node tests/e2e/visual/coverage.test.mjs` for the focused empty,
retired, iframe-only, missing and duplicate screenshot guard controls. These
controls are independent of the default E2E suite.

## Adding a renderer to the visual matrix

1. Ship it in the repository's content (`content/renderers/<group>/<id>/`).
2. Add a fixture to `FIXTURES` in `visual/fixtures.ts`
   (`id → filename, content, visible marker`), update `EXPECTED_VISUAL_COUNT`,
   and require its meaningful visible output before the screenshot.
3. `npm run test:visual:update` on Linux, commit the baseline.

`renderer-matrix.spec.ts` discovers renderers through `/api/renderers` and
needs no change.

## Adding a demo generator

Demo generators live in `playground/demo/generators/` and are seeded as
runners by `playground/demo/channel.yaml`. `smoke.spec.ts` asserts the exact
runner list, so add the new id to `DEMO_RUNNERS` in `helpers/playground.ts`
and give it a test in `demo-runners.spec.ts` that watches it to completion.
