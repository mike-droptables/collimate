import { test, expect } from "@playwright/test";
import { logVerifierRefusals, waitForPlaygroundReady } from "./helpers/playground";

/**
 * Perf budget: cold boot of the demo.
 *
 * A cold boot registers the service worker, downloads pyodide from the CDN,
 * installs the packages and the SDK wheel, unpacks the app and runs its
 * lifespan + channel seed. A regression here (a new dependency, a bigger
 * app tarball, slower startup) is the demo's hottest UX issue: visitors
 * leave while the overlay is still counting.
 *
 * The budget is generous because CI runners vary and the first CDN fetch can
 * spike (a warm local boot takes a few seconds). PERF_COLD_BUDGET_MS
 * overrides it for a known-warm matrix job.
 */

const COLD_BUDGET_MS = Number(process.env.PERF_COLD_BUDGET_MS) || 180_000;

test.describe.configure({ mode: "serial" });

test("cold boot completes within budget", async ({ browser }) => {
  test.setTimeout(COLD_BUDGET_MS + 15_000);
  // Fresh context: no service worker, no HTTP cache — a true cold boot.
  const ctx = await browser.newContext({ serviceWorkers: "allow", storageState: undefined });
  const page = await ctx.newPage();
  logVerifierRefusals(page);

  const start = Date.now();
  await page.goto("/playground/", { waitUntil: "load" });
  await waitForPlaygroundReady(page, COLD_BUDGET_MS);
  const elapsed = Date.now() - start;

  console.log(`cold boot: ${elapsed} ms (budget ${COLD_BUDGET_MS} ms)`);
  test.info().annotations.push({ type: "cold-boot-ms", description: String(elapsed) });
  expect(elapsed, "cold boot exceeded budget").toBeLessThanOrEqual(COLD_BUDGET_MS);

  await ctx.close();
});
