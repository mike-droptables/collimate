import { defineConfig } from "@playwright/test";

/**
 * Playwright against the Electron shell.
 *
 * This suite launches `desktop/dist/main.cjs` through Playwright's
 * `_electron` and lets the shell start the real api/ server itself. No
 * baseURL, no webServer, one worker — there is a single-instance lock, a
 * real port, and a real SQLite database at the other end of it.
 *
 * Run it with:
 *   xvfb-run -a npx playwright test -c playwright.electron.config.ts
 * (Electron needs a display; a headless CI runner needs the xvfb wrapper.
 * On a desktop with a session, drop the wrapper.)
 *
 * Prerequisites: `npm ci` then `node node_modules/electron/install.js` in
 * desktop/ (for the pinned Electron binary), a uv on
 * PATH, and a synced api/ — plus `api/static/` populated from
 * `frontend/dist` if you want the window to show the workbench rather
 * than the server's 404 document. The global setup builds dist/main.cjs
 * itself; see the desktop-e2e job in .github/workflows/test.yml for the
 * full sequence.
 */
export default defineConfig({
  testDir: "./tests/electron",
  testMatch: ["**/*.spec.ts"],
  globalSetup: "./tests/electron/global-setup.ts",

  // One shell, one server, one port: nothing here is parallel-safe.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  // A first run resolves the Python environment with uv, which on a cold
  // cache is most of the budget.
  timeout: 240_000,
  expect: { timeout: 30_000 },
  // No retries: a retry would relaunch the whole stack, and a flake here
  // is a real defect in startup or teardown rather than browser noise.
  retries: 0,

  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"]],

  // _electron contexts are traced explicitly by tests/electron/tracing.ts.
  outputDir: "test-results/electron",
});
