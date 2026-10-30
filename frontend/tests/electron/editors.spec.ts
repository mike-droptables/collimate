import { randomUUID } from "node:crypto";
import { rmSync } from "node:fs";
import { expect, request as playwrightRequest, test, type Frame, type Locator, type Page } from "@playwright/test";
import { createSeededChannel } from "../seedChannel";
import { launchShell, missingPrerequisite, workbenchWindow, type LaunchedShell } from "./desktop";

/**
 * The editors of plan 1011 (RS-03, RS-04), inside the real Electron shell.
 *
 * The headless suites drive these in Playwright's Chromium; this one drives
 * the shell's own window on the pinned Electron binary, against the server
 * the shell started itself:
 *
 * - RS-03 drag-insert: a drag of one of this cell's files from the Files
 *   pane onto the markdown editor (a same-origin sandbox.html iframe) reads
 *   `application/x-collimate-files` at drop time and writes an image
 *   reference, which the editor saves into the markdown.
 * - RS-03 caret-at-end: a press on the editor column below the last block
 *   puts the caret at the end of the document.
 * - RS-04 field-sizing: the text renderer (a native shadow-root renderer)
 *   sizes its textarea to its text with `field-sizing: content`, so a press
 *   below the text lands on the scroller and puts the caret at the end.
 * - RS-02/RS-03 style control: the corner-shown Style icon in the markdown
 *   editor (shown only within reach of its top-right corner) writes
 *   `renderer_styles.milkdown.theme` to the channel settings.
 *
 * Seeding is through the server's own API (POST /api/channels, the blob
 * stream and /import), as the renderer suites do.
 */

const blocked = missingPrerequisite();
test.skip(blocked !== null, `desktop e2e prerequisites are missing: ${blocked ?? ""}`);
test.describe.configure({ mode: "serial" });

// A 1x1 PNG.
const DOT_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);
const NOTES = "# Title\n\nA line.\n";
const PLAIN = "plain text\n";

let shell: LaunchedShell | null = null;
let page: Page;
let serverUrl = "";
let channel = "";
let cellId = "";

async function api(pathname: string, init?: RequestInit): Promise<Response> {
  return fetch(`${serverUrl}${pathname}`, init);
}

async function json<T>(response: Response): Promise<T> {
  const body = await response.text();
  expect(response.ok, `${response.status} ${body}`).toBe(true);
  return JSON.parse(body) as T;
}

async function streamBlob(bytes: Buffer): Promise<string> {
  const stored = await json<{ blob_hash: string }>(await api("/api/blob/stream", {
    method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: new Uint8Array(bytes),
  }));
  return stored.blob_hash;
}

async function fileText(pathInCell: string): Promise<string> {
  const response = await api(`/api/channels/${channel}/cells/${cellId}/files/${pathInCell}`);
  expect(response.ok, `GET ${pathInCell}: ${response.status}`).toBe(true);
  return response.text();
}

/** The markdown editor: the sandbox.html frame whose document holds a mounted Crepe. */
async function milkdownFrame(): Promise<Frame> {
  let found: Frame | undefined;
  await expect.poll(async () => {
    found = undefined;
    for (const frame of page.frames()) {
      if (!frame.url().includes("sandbox.html")) continue;
      try {
        if (!(await frame.locator(".milkdown .ProseMirror").count())) continue;
        // A pooled frame parked behind another tab still holds its editor.
        if (!(await (await frame.frameElement()).isVisible())) continue;
        found = frame;
        return true;
      } catch {
        // A pooled frame swapping its document; poll again.
      }
    }
    return false;
  }, { timeout: 90_000, message: "the milkdown editor never mounted in a sandbox.html frame" }).toBe(true);
  return found!;
}

function cell(): Locator {
  return page.locator(`[data-cell-id="${cellId}"]`);
}

function fileRow(pathInCell: string): Locator {
  return cell().locator(`[data-filenode-path="${pathInCell}"][data-filenode-kind="file"]`);
}

/** A row of the Files tree, expanding its folder first when the tree has it closed. */
async function visibleFileRow(pathInCell: string): Promise<Locator> {
  const row = fileRow(pathInCell);
  if (!(await row.isVisible()) && pathInCell.includes("/")) {
    const folder = pathInCell.slice(0, pathInCell.lastIndexOf("/"));
    await cell().locator(`[data-filenode-path="${folder}"]`).first().click();
  }
  await expect(row).toBeVisible();
  return row;
}

async function center(locator: Locator): Promise<{ x: number; y: number }> {
  const box = await locator.boundingBox();
  if (!box) throw new Error("element has no box");
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

test.beforeAll(async ({ browserName: _browserName }, info) => {
  shell = await launchShell();
  try {
    page = await workbenchWindow(shell.app);
    serverUrl = new URL(page.url()).origin;
  } catch (error) {
    await shell.trace.finish(info, shell.app.windows()[0], true);
    throw error;
  }

  // A new channel (the server makes it the active one), then one cell with
  // the three files, through the import route.
  const seeding = await playwrightRequest.newContext({ baseURL: serverUrl });
  try {
    channel = await createSeededChannel(seeding, `Electron editors ${Date.now()}`);
  } finally {
    await seeding.dispose();
  }
  const files = [
    { path: "notes.md", blob_hash: await streamBlob(Buffer.from(NOTES, "utf8")) },
    { path: "img/dot.png", blob_hash: await streamBlob(DOT_PNG) },
    { path: "plain.txt", blob_hash: await streamBlob(Buffer.from(PLAIN, "utf8")) },
  ];
  const imported = await json<{ cell_ids: string[] }>(await api(`/api/channels/${channel}/import`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ insert_index: null, stacks: [{ name: "", cells: [{ name: "Editors", files, dirs: [] }] }] }),
  }));
  cellId = imported.cell_ids[0]!;

  // The cell opens on notes.md in the milkdown editor, the Files pane on the left.
  const leaf = randomUUID(), tab = randomUUID();
  await json(await api(`/api/channels/${channel}/cell_display/${cellId}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      version: 1, root: { type: "leaf", id: leaf, tabs: [{ id: tab, kind: "file", path: "notes.md", rendererId: "milkdown" }], activeTabId: tab },
      activeLeafId: leaf,
      filesPane: { collapsed: false, side: "left", width: null, height: null, viewMode: "tree", panelFilters: { runsOnly: false, sortByRecency: false } },
      runsPane: { open: false, side: "right", width: null, height: null },
      pinnedTabCompact: false, history: null,
    }),
  }));

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(cell()).toBeVisible({ timeout: 60_000 });
});

test.beforeEach(async ({ browserName: _browserName }, info) => { await shell?.trace.begin(info); });
test.afterEach(async ({ browserName: _browserName }, info) => { await shell?.trace.finish(info, page); });

test.afterAll(async () => {
  if (shell) {
    await shell.app.close().catch(() => undefined);
    rmSync(shell.userDataRoot, { recursive: true, force: true });
    shell = null;
  }
});

test("RS-03 drag-insert: a Files pane row dropped on the markdown editor writes an image reference, saved __RS03_drag_insert", async () => {
  const frame = await milkdownFrame();
  const editor = frame.locator(".milkdown .ProseMirror");
  await expect(editor).toContainText("A line.");
  // The click-to-focus overlay of an unselected cell swallows the first press.
  const overlay = cell().locator("div.absolute.inset-0.z-10.cursor-pointer");
  if (await overlay.count()) await overlay.first().click();

  // Record what the frame's own drop sees, before the editor handles it.
  await frame.evaluate(() => {
    const record = window as Window & { cellFileDrop?: { types: string[]; payload: string } };
    window.addEventListener("drop", (event) => {
      const dt = event.dataTransfer;
      record.cellFileDrop = { types: dt ? [...dt.types] : [], payload: dt ? dt.getData("application/x-collimate-files") : "" };
    }, { capture: true, once: true });
  });

  const row = await visibleFileRow("img/dot.png");
  const from = await center(row);
  const target = await center(frame.locator(".milkdown .ProseMirror p").filter({ hasText: "A line." }));

  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(from.x + 14, from.y + 4, { steps: 4 });
  await page.mouse.move(target.x, target.y, { steps: 12 });
  await page.mouse.move(target.x + 2, target.y, { steps: 2 });
  await page.mouse.up();

  // Drop-time getData of the host-set type, inside the same-origin iframe.
  const dropped = await frame.evaluate(() =>
    (window as Window & { cellFileDrop?: { types: string[]; payload: string } }).cellFileDrop ?? null);
  expect(dropped, "RS-03 drag-insert: no drop reached the editor frame").not.toBeNull();
  expect(dropped!.types).toContain("application/x-collimate-files");
  expect(JSON.parse(dropped!.payload), "RS-03 drag-insert: drop-time getData of application/x-collimate-files")
    .toEqual({ cellId, paths: ["img/dot.png"] });

  // The drop read the host-set type in the iframe and inserted the image node.
  await expect.poll(async () => {
    const images = await frame.locator(".milkdown img").count();
    const text = await editor.evaluate((node) => node.innerHTML);
    return images > 0 || text.includes("dot.png");
  }, { message: "RS-03 drag-insert: the editor gained no image after the drop" }).toBe(true);

  // The editor's save reaches the server as a markdown image reference.
  await expect.poll(() => fileText("notes.md"), {
    timeout: 20_000, message: "RS-03 drag-insert: the saved markdown has no image reference",
  }).toMatch(/!\[[^\]]*\]\(img\/dot\.png\)/);
  const saved = await fileText("notes.md");
  expect(saved).toContain("![");
  expect(saved).toContain("dot.png");
  expect(saved).toContain("A line.");
});

test("RS-03 caret-at-end: a press below the last block puts the caret at the document's end __RS03_caret_at_end", async () => {
  const frame = await milkdownFrame();
  const editor = frame.locator(".milkdown .ProseMirror");
  const host = await frame.frameElement();
  const frameBox = await host.boundingBox();
  const last = await frame.locator(".milkdown .ProseMirror > *").last().boundingBox();
  if (!frameBox || !last) throw new Error("no editor geometry");
  // Well below the content, inside the frame, clear of the bottom strip.
  const y = Math.max(last.y + last.height + 80, frameBox.y + frameBox.height * 0.6);
  expect(y, "the editor needs room below its last block").toBeLessThan(frameBox.y + frameBox.height - 40);
  const x = last.x + Math.min(40, last.width / 2);
  // The press must land on the column ground, not on a block.
  const hit = await frame.evaluate(({ x, y }) => {
    const el = document.elementFromPoint(x, y);
    return el ? `${el.tagName}.${el.className}` : "";
  }, { x: x - frameBox.x, y: y - frameBox.y });
  expect(hit).not.toMatch(/^(P|H1|IMG)\./);

  // The caret starts at the top of the document, so the press must move it.
  await frame.locator(".milkdown .ProseMirror h1").click({ position: { x: 2, y: 4 } });
  await page.keyboard.press("Home");
  await page.mouse.click(x, y);
  await page.keyboard.type(" tail");
  await expect.poll(async () => (await editor.innerText()).trimEnd(), {
    message: "RS-03 caret-at-end: the typed text did not land at the document's end",
  }).toMatch(/tail$/);
  await expect.poll(async () => (await fileText("notes.md")).trimEnd(), { timeout: 20_000 }).toMatch(/tail$/);
});

test("RS-04 field-sizing: the text renderer's area sizes to its text and a press below it types at the end __RS04_field_sizing", async () => {
  expect(await page.evaluate(() => CSS.supports("field-sizing: content")),
    "RS-04 field-sizing: the Electron window must support field-sizing").toBe(true);

  const row = await visibleFileRow("plain.txt");
  await row.click();
  // CSS locators pierce the native renderer's open shadow root.
  const area = cell().locator("textarea[data-text-area]");
  await expect(area).toBeVisible({ timeout: 60_000 });
  await expect(area).toHaveValue(PLAIN);

  const geometry = await area.evaluate((node) => {
    const scroller = node.parentElement!;
    return {
      sizing: getComputedStyle(node).getPropertyValue("field-sizing"),
      area: node.getBoundingClientRect().toJSON() as DOMRect,
      scroller: scroller.getBoundingClientRect().toJSON() as DOMRect,
    };
  });
  expect(geometry.sizing, "RS-04 field-sizing: computed field-sizing").toBe("content");
  // Sized to its two lines, not stretched over the scroller.
  expect(geometry.area.height, "RS-04 field-sizing: the area is as tall as its text").toBeLessThan(geometry.scroller.height - 60);

  const areaBox = await area.boundingBox();
  const scrollerBox = await area.locator("xpath=..").boundingBox();
  if (!areaBox || !scrollerBox) throw new Error("no text renderer geometry");
  const below = { x: areaBox.x + 40, y: Math.min(areaBox.y + areaBox.height + 60, scrollerBox.y + scrollerBox.height - 10) };
  expect(below.y).toBeGreaterThan(areaBox.y + areaBox.height);
  await page.mouse.click(below.x, below.y);
  await page.keyboard.type(" end");
  await expect(area, "RS-04 field-sizing: a press below the text puts the caret at its end").toHaveValue(/ end$/);
  expect(await area.inputValue()).toBe(`${PLAIN} end`);
  await expect.poll(() => fileText("plain.txt"), { timeout: 20_000 }).toBe(`${PLAIN} end`);
});

test("RS-03 style control: the corner-shown Style icon in the markdown editor stores the theme __RS03_style_control", async () => {
  const row = await visibleFileRow("notes.md");
  await row.click();
  // A single click opens after the double-click window: wait for the tab.
  await expect(row).toHaveAttribute("aria-current", "true");
  await expect(cell().locator("textarea[data-text-area]")).toBeHidden();
  const frame = await milkdownFrame();
  const icon = frame.getByRole("button", { name: /style/i });
  // getByRole skips hidden elements, so the hidden state is read off the button itself.
  const iconElement = frame.locator('[data-collimate-style-control] button[aria-label="Style"]');
  await expect(iconElement).toHaveCount(1);

  // At rest the icon is transparent (opacity 0, still focusable by Tab); over
  // the editor away from its corner it stays so; within reach of the icon it
  // shows. Playwright counts opacity 0 as visible, so opacity is asserted.
  const away = await center(row);
  await page.mouse.move(away.x, away.y);
  await expect(iconElement, "RS-03 style control: hidden while the pointer is elsewhere").toHaveCSS("opacity", "0");
  const host = await frame.frameElement();
  const box = await host.boundingBox();
  if (!box) throw new Error("no editor frame box");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 3, { steps: 5 });
  await expect(iconElement, "RS-03 style control: hidden over the editor away from its corner").toHaveCSS("opacity", "0");
  await page.mouse.move(box.x + box.width - 40, box.y + 30, { steps: 5 });
  await expect(iconElement, "RS-03 style control: shown near its corner").toHaveCSS("opacity", "1");

  await icon.click();
  const dialog = frame.getByRole("dialog", { name: "Style" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("combobox", { name: "Theme" }).selectOption("classic");

  await expect.poll(async () => {
    const settings = await json<{ renderer_styles?: Record<string, { theme?: string | null }> }>(
      await api(`/api/channels/${channel}/settings`));
    return settings.renderer_styles?.milkdown?.theme ?? null;
  }, { message: "RS-03 style control: renderer_styles.milkdown.theme was not stored" }).toBe("classic");
});
