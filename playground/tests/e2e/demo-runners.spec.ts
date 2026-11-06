import { test, expect } from "@playwright/test";
import {
  gotoPlayground,
  demoState,
  cellByName,
  cellById,
  cellFiles,
  cellFileText,
  runFromPanel,
  startRun,
  waitForRunEnd,
  runRecord,
  trackPhases,
  waitForPhase,
  setRunnerSettings,
  focusCell,
  terminalOutput,
  activeRuns,
  runLogPaths,
  hasRunFiles,
} from "./helpers/playground";

/**
 * Every demo runner, run from the runners panel on the cell it is meant for, watched
 * to completion in its live renderer. The two open-ended windows (the
 * terminal's keep-open, the chat's idle timeout) are shortened through the
 * runner editor API so the specs finish in seconds; the scripted parts play
 * at their shipped pace.
 *
 * A run's live surfaces are UI files in its own folder, `_runs/<label>/`:
 * present while the run lives, swept into the run's Logs when it ends. A run
 * never touches the cell's pin (the user's); it reveals files instead.
 */

test.describe("demo runners", () => {
  test("Terminal session: an ANSI shell in the terminal renderer, echoes typed input, completes", async ({ page }) => {
    await gotoPlayground(page);
    await trackPhases(page);
    await setRunnerSettings(page, "terminal-session", { keep_open_seconds: 8 });
    const welcome = await cellByName(page, "Welcome");

    const run = await runFromPanel(page, welcome.id, "Terminal session");
    const pointer = `${run.folder}/terminal/session.terminal`;
    await expect.poll(() => cellFiles(page, welcome.id), { timeout: 30_000 }).toContain(pointer);
    await expect(page.locator(".xterm").first()).toBeAttached({ timeout: 30_000 });

    await waitForPhase(page, run.run_id, "awaiting_input");
    await focusCell(page);
    await page.locator(".xterm").first().click();
    await page.keyboard.type("ls -la", { delay: 40 });
    await page.keyboard.press("Enter");
    await expect.poll(() => terminalOutput(page), { timeout: 5_000 }).toContain("commands are not executed here");

    const output = await terminalOutput(page);
    expect(output).toContain("git clone");
    expect(output).toContain("39 passed");
    expect(output).toContain("1 skipped");
    expect(output, "typed input is echoed").toContain("ls -la");
    expect(output, "ANSI colour").toContain("\x1b[32m");

    const final = await waitForRunEnd(page, run.run_id);
    expect(final.status).toBe("completed");
    // A run on an existing cell keeps its name, its files and the user's
    // pin; its folder goes with it, into the run's Logs.
    const cell = await cellById(page, welcome.id);
    expect(cell.name).toBe("Welcome");
    expect(cell.pinned).toBe("README.md");
    await expect.poll(async () => hasRunFiles(await cellFiles(page, welcome.id)), { timeout: 10_000 }).toBe(false);
    expect(await cellFiles(page, welcome.id)).toContain("README.md");
    expect(await runLogPaths(page, run.run_id)).toContain(pointer);
  });

  test("Chat assistant: streams the scripted turns, answers a typed message, completes", async ({ page }) => {
    await gotoPlayground(page);
    await trackPhases(page);
    await setRunnerSettings(page, "chat-assistant", { idle_timeout_seconds: 8 });
    const welcome = await cellByName(page, "Welcome");

    const run = await runFromPanel(page, welcome.id, "Chat assistant");
    // getByText pierces the renderer's shadow root; the first reply streams
    // word by word, so its opening words are visible while the turn is live.
    await expect(page.getByText(/Everything you see is the real Collimate app/).first()).toBeVisible({ timeout: 40_000 });
    await expect(page.getByText(/The visitor is asking about the demo itself/).first()).toBeAttached();

    await waitForPhase(page, run.run_id, "awaiting_input", 90_000);
    await focusCell(page);
    const composer = page.locator("textarea").filter({ visible: true }).last();
    await expect(composer).toBeEnabled({ timeout: 10_000 });
    await composer.click();
    await page.keyboard.type("Is a real model behind this?");
    await page.keyboard.press("Enter");
    await expect(page.getByText(/No model is behind this chat/).first()).toBeVisible({ timeout: 20_000 });

    // The transcript is a UI file of the run while it lives.
    const transcriptPath = `${run.folder}/chat/main.json`;
    const transcript = JSON.parse(await cellFileText(page, welcome.id, transcriptPath)) as {
      messages: Array<{ role: string; content: string }>;
    };
    const roles = transcript.messages.map((m) => m.role);
    expect(roles.slice(0, 4)).toEqual(["user", "assistant", "user", "assistant"]);
    expect(transcript.messages.some((m) => m.role === "user" && m.content === "Is a real model behind this?")).toBe(true);

    const final = await waitForRunEnd(page, run.run_id);
    expect(final.status).toBe("completed");
    const cell = await cellById(page, welcome.id);
    expect(cell.name).toBe("Welcome");
    expect(cell.pinned).toBe("README.md");
    await expect.poll(async () => hasRunFiles(await cellFiles(page, welcome.id)), { timeout: 10_000 }).toBe(false);
    expect(await cellFiles(page, welcome.id)).toContain("README.md");
    expect(await runLogPaths(page, run.run_id)).toEqual(
      expect.arrayContaining([transcriptPath, `${run.folder}/chat/conversation.chat`]),
    );
  });

  test("Modify files: edits the Sample notes in place, one version per file, ends on CHANGES.md", async ({ page }) => {
    await gotoPlayground(page);
    const notes = await cellByName(page, "Sample notes");

    const run = await runFromPanel(page, notes.id, "Modify files");
    const final = await waitForRunEnd(page, run.run_id);
    expect(final.status).toBe("completed");

    const cell = await cellById(page, notes.id);
    expect(cell.name).toBe("Sample notes");
    expect(cell.pinned).toBe("trip-plan.md");
    await expect.poll(async () => hasRunFiles(await cellFiles(page, notes.id)), { timeout: 10_000 }).toBe(false);
    expect(await cellFiles(page, notes.id)).toEqual(
      expect.arrayContaining(["CHANGES.md", "budget.txt", "packing-list.md", "trip-plan.md"]),
    );
    expect((await cellFileText(page, notes.id, "packing-list.md")).match(/^- \[ \] /gm)?.length).toBe(7);
    expect(await cellFileText(page, notes.id, "trip-plan.md")).toContain("> **Summary:**");
    expect(await cellFileText(page, notes.id, "CHANGES.md")).toContain("```diff");
  });

  test("Two runs share a cell; Stop ends the selected one within a second and leaves the other", async ({ page }) => {
    await gotoPlayground(page);
    await trackPhases(page);
    const welcome = await cellByName(page, "Welcome");

    const terminal = await runFromPanel(page, welcome.id, "Terminal session");
    await waitForPhase(page, terminal.run_id, "running");
    await setRunnerSettings(page, "chat-assistant", { idle_timeout_seconds: 8 });
    const chat = await startRun(page, welcome.id, "chat-assistant");
    expect(chat.label).not.toBe(terminal.label);
    await expect
      .poll(async () => (await activeRuns(page)).filter((r) => r.cell_id === welcome.id).map((r) => r.folder).sort())
      .toEqual([chat.folder, terminal.folder].sort());
    await expect
      .poll(() => cellFiles(page, welcome.id), { timeout: 30_000 })
      .toEqual(expect.arrayContaining([`${terminal.folder}/terminal/session.terminal`, `${chat.folder}/chat/conversation.chat`]));

    // The cell's run light opens its status pane, whose status view lists
    // one row per live run; the terminal's row opens its drilldown, which
    // holds the run's Stop.
    const welcomeCell = page.locator(`[data-cell-id="${welcome.id}"]`);
    const light = welcomeCell.getByTestId("run-light");
    await expect(light).toHaveAttribute("data-run-count", "2", { timeout: 10_000 });
    await light.click();
    const pane = welcomeCell.locator("[data-runs-pane]");
    const row = pane.locator(`[data-testid="status-run-row"][data-run-id="${terminal.run_id}"]`);
    await row.click();
    const drilldown = pane.locator(`[data-testid="run-drilldown"][data-run-id="${terminal.run_id}"]`);
    await expect(drilldown).toBeVisible();
    const stop = drilldown.getByTestId("run-stop");
    const pressed = Date.now();
    await stop.click();
    // The timeline is cancelled on the spot (tens of milliseconds); what the
    // clock measures here is the click plus the API round trips through the
    // service worker into a runtime sharing the CPU with the other workers,
    // so the budget is "about a second" with that overhead on top.
    let final = await runRecord(page, terminal.run_id);
    while (final.status === "running" && Date.now() - pressed < 5_000) {
      await page.waitForTimeout(50);
      final = await runRecord(page, terminal.run_id);
    }
    const elapsed = Date.now() - pressed;
    expect(final.status).toBe("stopped");
    expect(elapsed, `stop took ${elapsed} ms`).toBeLessThan(2_500);

    // The other run carries on, its folder untouched.
    // A stopped row remains active until its owned run-end cleanup is drained.
    await expect
      .poll(async () => (await activeRuns(page)).map((r) => r.run_id), { timeout: 10_000 })
      .toEqual([chat.run_id]);
    await expect
      .poll(async () => (await cellFiles(page, welcome.id)).some((f) => f.startsWith(`${terminal.folder}/`)), { timeout: 10_000 })
      .toBe(false);
    expect(await cellFiles(page, welcome.id)).toContain(`${chat.folder}/chat/conversation.chat`);
    expect((await waitForRunEnd(page, chat.run_id)).status).toBe("completed");
    await expect.poll(async () => hasRunFiles(await cellFiles(page, welcome.id)), { timeout: 10_000 }).toBe(false);
  });
});
