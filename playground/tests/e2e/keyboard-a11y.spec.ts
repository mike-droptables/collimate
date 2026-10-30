import { test, expect } from "@playwright/test";
import { AxeBuilder } from "@axe-core/playwright";
import { gotoPlayground } from "./helpers/playground";

/**
 * Keyboard-only accessibility: the demo's own chrome (the "Browser demo"
 * badge and its card) and the in-cell runners panel must be reachable and
 * dismissable without a mouse, and never trap focus.
 */

test.describe("keyboard-only a11y: the demo badge and card", () => {
  test("badge accepts focus and opens the card on Enter", async ({ page }) => {
    await gotoPlayground(page);
    const badge = page.locator("#collimate-playground-badge");
    await badge.waitFor({ timeout: 10_000 });
    await badge.focus();
    await expect(badge).toBeFocused();
    await page.keyboard.press("Enter");
    const card = page.locator("#collimate-playground-card");
    await expect(card).toBeVisible({ timeout: 5_000 });
    // Focus moves into the dialog, onto its one control.
    await expect(card.getByRole("button", { name: "Got it" })).toBeFocused();
  });

  test("Escape dismisses the card and returns focus to the badge", async ({ page }) => {
    await gotoPlayground(page);
    const badge = page.locator("#collimate-playground-badge");
    await badge.waitFor({ timeout: 10_000 });
    await badge.focus();
    await page.keyboard.press("Enter");
    const card = page.locator("#collimate-playground-card");
    await expect(card).toBeVisible({ timeout: 5_000 });
    await page.keyboard.press("Escape");
    await expect(card).toBeHidden({ timeout: 3_000 });
    await expect(badge).toBeFocused();
  });

  test("the card has no serious axe violations", async ({ page }) => {
    await gotoPlayground(page);
    await page.locator("#collimate-playground-badge").click();
    await page.locator("#collimate-playground-card").waitFor({ timeout: 5_000 });
    const results = await new AxeBuilder({ page })
      .include("#collimate-playground-card")
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();
    const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(serious, serious.map((v) => `  ${v.id} [${v.impact}] — ${v.help}`).join("\n")).toEqual([]);
  });
});

test.describe("keyboard-only a11y: the in-cell runners panel", () => {
  test("Alt+Shift+N opens the runners panel in a new cell with the search focused; Escape closes it", async ({ page }) => {
    await gotoPlayground(page);
    // The welcome cell's markdown editor (in its sandbox iframe) may hold
    // focus right after boot; a keyboard user lands on the main document
    // first — the badge is the nearest Tab stop outside every cell.
    await page.locator("#collimate-playground-badge").focus();
    await page.keyboard.press("Alt+Shift+KeyN");

    const search = page.getByRole("combobox", { name: "Search runners" });
    await search.waitFor({ state: "visible", timeout: 5_000 });
    await expect(search).toBeFocused();

    // The panel lives inside a cell and is not modal: the rest of the
    // page stays reachable.
    const surface = page.locator('[data-pick-surface][data-cell-panel="runners"]').first();
    await expect(surface.locator("xpath=ancestor::*[@data-cell-id][1]")).toHaveCount(1);
    expect(await surface.getAttribute("aria-modal")).toBeNull();

    // The three demo runners are listed; the arrows move the selection
    // while the search keeps the keyboard.
    const options = page.getByRole("listbox", { name: "Runners" }).getByRole("option");
    await expect(options).toHaveCount(3);
    // Select the intended runner with arrows, independent of catalog order.
    const terminalIndex = await options.evaluateAll((rows) =>
      rows.findIndex((row) => row.id.endsWith("-option-terminal-session")));
    expect(terminalIndex).toBeGreaterThanOrEqual(0);
    for (let i = 0; i < 3; i++) await page.keyboard.press("ArrowUp");
    for (let i = 0; i < terminalIndex; i++) await page.keyboard.press("ArrowDown");
    const terminal = options.nth(terminalIndex);
    await expect(terminal).toHaveAttribute("aria-selected", "true");
    await expect(terminal).toContainText("Terminal session");
    // The panel only runs: no mode strip, and its primary runs the selection.
    await expect(surface.getByRole("group", { name: "Mode" })).toHaveCount(0);
    await expect(surface.getByRole("button", { name: /Run ‘Terminal session’/ })).toBeVisible();
    await expect(search).toBeFocused();

    await search.focus();
    await page.keyboard.press("Escape");
    await expect(search).toBeHidden({ timeout: 3_000 });
  });
});
