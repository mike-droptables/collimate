import { test, expect } from "@playwright/test";
import { gotoPlayground, demoState, api, channelSettingsLoaded } from "./helpers/playground";

/**
 * The demo runs ONE channel and nothing that needs a machine. The shim's
 * guard answers those routes itself (playground/shim/collimate_playground/
 * boot.py) and the build's anchored patches hide the matching controls.
 */

test.describe("single channel + browser-demo guards", () => {
  test("channel create / delete / rename answer 400 with a friendly detail", async ({ page }) => {
    await gotoPlayground(page);
    const { channelId } = await demoState(page);
    const detail = { detail: "The demo runs a single channel" };

    expect(await api(page, "POST", "/channels?name=Another")).toEqual({ status: 400, body: detail });
    expect(await api(page, "POST", "/channels?name=Another", { runners: [] })).toEqual({ status: 400, body: detail });
    expect(await api(page, "DELETE", `/channels/${channelId}`)).toEqual({ status: 400, body: detail });
    expect(await api(page, "PUT", `/channels/${channelId}/rename?name=Renamed`)).toEqual({ status: 400, body: detail });

    const after = await demoState(page);
    expect(after.channelName).toBe("Demo");
  });

  test("account writes answer 501; accounts read as empty", async ({ page }) => {
    await gotoPlayground(page);
    const detail = { detail: "Not available in the browser demo" };

    for (const [method, path] of [
      ["POST", "/accounts"],
      ["DELETE", "/accounts/anything"],
    ] as const) {
      const r = await api(page, method, path, {});
      expect(r, `${method} ${path}`).toEqual({ status: 501, body: detail });
    }
    expect(await api(page, "GET", "/accounts")).toEqual({ status: 200, body: [] });
  });

  test("the channel menu offers no New / Rename / Delete", async ({ page }) => {
    await gotoPlayground(page);
    await page.locator('[data-top-bar] button[aria-haspopup="menu"]').first().click();
    const entries = await page.evaluate(() =>
      Array.from(document.querySelectorAll("button"))
        .map((b) => b.textContent?.trim() ?? "")
        .filter((t) => /new channel|rename current|delete current/i.test(t)),
    );
    expect(entries).toEqual([]);
    await page.keyboard.press("Escape");
  });

  test("Settings offers no Key manager, on the settings root or in the Commands pane's Settings group", async ({ page }) => {
    const settingsLoaded = channelSettingsLoaded(page);
    await gotoPlayground(page);
    // The app adopts the Commands pane's saved state from the channel's
    // settings document when it lands, closing a pane opened before then.
    // Drive the pane after that; an adoption still in flight can only close
    // the pane mid-round (never pass one), so a closed pane retries the round.
    await settingsLoaded;
    const pane = page.locator("[data-command-pane]");
    await expect(async () => {
      await page.keyboard.press("ControlOrMeta+Comma");
      await pane.and(page.locator('[data-pane-view="settings"]')).waitFor({ timeout: 5_000 });
      // The section rows carry their ids (the Settings file rows carry none);
      // Servers is desktop-only.
      const sections = await pane
        .locator('[data-settings-row]:not([data-settings-row=""])')
        .evaluateAll((rows) => rows.map((row) => row.getAttribute("data-settings-row")));
      expect(sections).toEqual(["editing", "theme"]);
      await page.keyboard.press("Escape");
      await pane.and(page.locator('[data-pane-view="commands"]')).waitFor({ timeout: 5_000 });
      const settingsRows = await pane
        .locator('[data-command^="settings:"]')
        .evaluateAll((rows) => rows.map((row) => row.getAttribute("data-command")));
      expect(settingsRows).toEqual(["settings:editing", "settings:theme"]);
      await expect(pane.locator('[data-command="resetshortcuts"]')).toHaveCount(1);
    }).toPass({ timeout: 30_000 });
    await page.keyboard.press("Escape");
  });
});
