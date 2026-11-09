import { defineConfig, devices } from "@playwright/test";

const sitePort = Number(process.env.COLLIMATE_E2E_PORT) || 4321;

export default defineConfig({
  testDir: "./tests/e2e",
  // Playwright expects .spec.ts by default — keep it explicit so a stray
  // *.test.ts never gets picked up.
  testMatch: ["**/*.spec.ts"],
  // Visual-regression specs live under tests/e2e/visual/ and are opt-in
  // (VISUAL=1). Baselines are platform-sensitive and the snapshot
  // directory needs seeding, so default e2e runs exclude them.
  testIgnore: process.env.VISUAL ? [] : ["**/visual/**"],
  // Every spec boots the Python runtime first (a few seconds warm, up to a
  // minute cold on CI) and most then watch a scripted run of 20–40 s to
  // completion; Playwright's 30 s default would cut the runs short.
  timeout: 150_000,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  maxFailures: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI
    ? [["github"], ["html", { open: "never" }]]
    : [["list"]],

  use: {
    baseURL: `http://127.0.0.1:${sitePort}`,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },

  expect: {
    toHaveScreenshot: {
      // Minor sub-pixel noise in font AA is unavoidable even in a
      // pinned container image. 1% is tight enough to catch real
      // layout regressions but tolerates those diffs.
      maxDiffPixelRatio: 0.01,
      threshold: 0.2,
      animations: "disabled",
    },
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  // The specs drive the assembled demo site in dist/ — the exact output
  // build.sh produces and CI deploys. The in-browser Python stack only
  // exists in that output, so there is no dev-server mode and no backend
  // process to start. serve.sh is the one static-server recipe: it turns
  // serve's clean URLs off (the 301 from sandbox.html to /sandbox drops the
  // query string the renderer sandbox is parameterised with).
  webServer: {
    command: `PORT=${sitePort} bash serve.sh`,
    cwd: __dirname,
    url: `http://127.0.0.1:${sitePort}/playground/`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
