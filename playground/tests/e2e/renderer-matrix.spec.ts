import { test, expect } from "@playwright/test";
import {
  gotoPlayground,
  listRenderers,
  fetchRendererSource,
  loadNativeRenderer,
  brokenRewriteStillFails,
} from "./helpers/playground";

/**
 * Renderer import matrix — every renderer advertised by /api/renderers
 * (the SDK's real renderers, vendored into the demo) must:
 *   1. serve its canonical compiled bundle (200 + nonempty body + module.js
 *      X-Entrypoint header and current source-identity ETag)
 *   2. for `embed: native`: load via the NativeRenderer Blob import
 *      pipeline and export a `mount()` function
 *   3. for `embed: iframe`: have a resolvable sandbox.html at
 *      /playground/sandbox.html (the shell loads the renderer inside it)
 *
 * Autoscales as renderers are added to the SDK.
 */

test.describe("renderer matrix", () => {
  test("every renderer serves its source", async ({ page }) => {
    await gotoPlayground(page);
    const renderers = await listRenderers(page);
    expect(renderers.length).toBeGreaterThan(0);

    const failures: string[] = [];
    for (const r of renderers) {
      const info = await fetchRendererSource(page, r.id);
      if (info.status !== 200) {
        failures.push(`${r.id}: status=${info.status}`);
        continue;
      }
      if (info.entrypoint !== "module.js") {
        failures.push(`${r.id}: unexpected X-Entrypoint header ${info.entrypoint}`);
        continue;
      }
      if (info.etag !== `"${r.content_hash}"` || info.sourceHash !== r.content_hash) {
        failures.push(`${r.id}: compiled source identity mismatch`);
      }
      if (info.bodyLen < 50) {
        failures.push(`${r.id}: source body too short (${info.bodyLen}b)`);
      }
    }

    expect(
      failures,
      failures.length ? `\n  - ${failures.join("\n  - ")}` : "",
    ).toEqual([]);
  });

  test("every embed=native renderer loads via the NativeRenderer pipeline", async ({
    page,
  }) => {
    await gotoPlayground(page);
    const renderers = await listRenderers(page);
    const natives = renderers.filter((r) => r.embed === "native");
    expect(
      natives.length,
      "expected at least one native renderer (text ships by default)",
    ).toBeGreaterThan(0);

    const failures: string[] = [];
    for (const r of natives) {
      const result = await loadNativeRenderer(page, r.id);
      if (!result.ok) {
        failures.push(`${r.id}: ${JSON.stringify(result)}`);
        continue;
      }
      if (!result.hasMount) {
        failures.push(
          `${r.id}: module exports ${JSON.stringify(result.keys)} but no mount()`,
        );
      }
    }

    expect(
      failures,
      failures.length ? `\n  - ${failures.join("\n  - ")}` : "",
    ).toEqual([]);
  });

  test("every embed=iframe renderer has a reachable sandbox shell", async ({
    page,
  }) => {
    await gotoPlayground(page);
    const renderers = await listRenderers(page);
    const iframes = renderers.filter((r) => r.embed === "iframe");
    if (iframes.length === 0) {
      test.info().annotations.push({
        type: "info",
        description: "no iframe-embed renderers advertised; skipping",
      });
      return;
    }

    // Sandbox host resolves once; every iframe renderer shares it.
    const { sandboxPath, sandboxStatus } = await page.evaluate(async () => {
      const url = new URL("sandbox.html", window.location.href);
      const r = await fetch(url);
      return { sandboxPath: url.pathname, sandboxStatus: r.status };
    });
    expect(sandboxPath).toBe("/playground/sandbox.html");
    expect(sandboxStatus).toBe(200);

    // Plus each renderer's source must still resolve (the iframe
    // re-fetches it on mount).
    const failures: string[] = [];
    for (const r of iframes) {
      const info = await fetchRendererSource(page, r.id);
      if (info.status !== 200) {
        failures.push(`${r.id}: source status=${info.status}`);
      }
    }
    expect(failures).toEqual([]);
  });

  test("catalog advertises the renderers the demo depends on", async ({ page }) => {
    await gotoPlayground(page);
    const ids = (await listRenderers(page)).map((r) => r.id);
    expect(ids).toEqual(expect.arrayContaining(["text", "milkdown", "terminal", "assistant"]));
  });

  test("the old broken apiBase rewrite still fails (bug-signature pin)", async ({ page }) => {
    // NativeRenderer must resolve `..` against location.href, never concatenate
    // it onto location.origin; if a refactor reintroduces the concat, the
    // matrix above would still pass while every renderer broke in the app.
    await gotoPlayground(page);
    const result = await brokenRewriteStillFails(page);
    expect(result.fails, `expected the broken rewrite to fail, got ${JSON.stringify(result)}`).toBe(true);
    expect(result.error ?? "").toMatch(/module specifier|Invalid URL/i);
  });
});
