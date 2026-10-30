import { randomUUID } from "node:crypto";
import { rmSync } from "node:fs";
import { expect, request as playwrightRequest, test, type APIRequestContext, type Frame, type Page } from "@playwright/test";
import { createSeededChannel } from "../seedChannel";
import { launchShell, missingPrerequisite, workbenchWindow, type LaunchedShell } from "./desktop";

/**
 * Plan row FP-05 (files pane clipboard paste) inside the real Electron shell.
 *
 * The unit tests drive `useFilesPanePaste` with synthetic ClipboardEvents and
 * a stubbed `navigator.clipboard`; headless Chromium has no OS clipboard worth
 * the name. This suite puts real bytes on the OS clipboard from the main
 * process (Electron's `clipboard` module), presses the real Ctrl+V in the
 * shell's window (the application menu's paste role) and reads back what the
 * server stored. It also proves the context menu's "Paste here" can use the
 * async Clipboard API in the shell (the workbench session grants
 * clipboard-read), and that a paste inside the markdown editor's iframe stays
 * the editor's.
 *
 * The OS clipboard is shared with the desktop session: the text on it before
 * the suite is put back afterwards.
 */

const blocked = missingPrerequisite();
test.skip(blocked !== null, `desktop e2e prerequisites are missing: ${blocked ?? ""}`);

test.describe.configure({ mode: "serial" });

const PNG_2X2_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEUlEQVR4nGP4z8DA8B+MgBgAHfAD/dPQfSYAAAAASUVORK5CYII=";
const PNG_MAGIC = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const PASTED_MARKDOWN = "# Pasted\n\n- item\n";
const KEYBOARD_HINT = /Press Ctrl\+V in the files pane/;

let shell: LaunchedShell | null = null;
let workbench: Page;
let api: APIRequestContext | null = null;
let channel = "";
let cellId = "";
let savedClipboard: string | null = null;

/**
 * Replace the OS clipboard with `text` from the main process. Electron's
 * clipboard is the async W3C-shaped API: the write is awaited so it has landed
 * before the test presses Ctrl+V.
 */
async function putClipboardText(text: string): Promise<void> {
  await shell!.app.evaluate(({ clipboard }, text) => clipboard.writeText(text), text);
}

async function filePaths(): Promise<string[]> {
  const response = await api!.get(`/api/channels/${channel}/cells/${cellId}/file-paths`);
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).paths as string[];
}

/** The AddFileDialog the paste opens: a dialog headed "Paste <kind> in". */
function pastePrompt(kind?: "image" | "text") {
  return workbench.locator('[role="dialog"]').filter({ hasText: kind ? new RegExp(`paste ${kind} in`, "i") : /paste (image|text|file) in/i });
}

const notesRow = () => workbench.locator(`[data-cell-id="${cellId}"] [data-filenode-path="notes.md"]`).first();

/** Click the notes row so the keyboard focus is a (non-editable) tree row. */
async function focusTree(): Promise<void> {
  await notesRow().click();
  await expect.poll(() => workbench.evaluate(() => document.activeElement?.getAttribute("data-filenode-path") ?? null)).toBe("notes.md");
}

/** The milkdown iframe (sandbox.html) showing notes.md, once its editor has mounted. */
async function notesEditorFrame(): Promise<Frame> {
  let found: Frame | undefined;
  await expect.poll(async () => {
    for (const frame of workbench.frames()) {
      if (!frame.url().includes("renderer=milkdown") || !frame.url().includes("filename=notes.md")) continue;
      const shown = frame.locator(".ProseMirror").filter({ hasText: "Shell paste fixture." });
      if (await shown.count() && await shown.first().isVisible()) { found = frame; return true; }
    }
    return false;
  }, { message: "the milkdown editor shows notes.md" }).toBe(true);
  return found!;
}

test.beforeAll(async ({ browserName: _browserName }, info) => {
  shell = await launchShell();
  try {
    workbench = await workbenchWindow(shell.app);
  } catch (error) {
    await shell.trace.finish(info, shell.app.windows()[0], true);
    throw error;
  }
  savedClipboard = await shell.app.evaluate(({ clipboard }) => clipboard.readText());
  api = await playwrightRequest.newContext({ baseURL: new URL(workbench.url()).origin });

  // The channel, one cell holding notes.md, and a display with the files pane
  // shown on the left and notes.md open in the milkdown editor.
  channel = await createSeededChannel(api, `Electron paste ${Date.now()}`);
  expect((await api.post(`/api/channels/${channel}/select`)).ok()).toBe(true);
  const blob = await api.post("/api/blob/stream", {
    data: Buffer.from("# Notes\n\nShell paste fixture.\n", "utf8"),
    headers: { "Content-Type": "application/octet-stream" },
  });
  expect(blob.ok(), await blob.text()).toBe(true);
  const imported = await api.post(`/api/channels/${channel}/import`, { data: {
    insert_index: null,
    stacks: [{ name: "Paste", cells: [{ name: "Paste cell", files: [{ path: "notes.md", blob_hash: (await blob.json()).blob_hash }], dirs: [] }] }],
  } });
  expect(imported.ok(), await imported.text()).toBe(true);
  cellId = (await imported.json()).cell_ids[0] as string;
  const leaf = randomUUID(), tab = randomUUID();
  const display = await api.put(`/api/channels/${channel}/cell_display/${cellId}`, { data: {
    version: 1, root: { type: "leaf", id: leaf, tabs: [{ id: tab, kind: "file", path: "notes.md", rendererId: "milkdown" }], activeTabId: tab },
    activeLeafId: leaf,
    filesPane: { collapsed: false, side: "left", width: null, height: null, viewMode: "tree", panelFilters: { runsOnly: false, sortByRecency: false } },
    runsPane: { open: false, side: "right", width: null, height: null },
    pinnedTabCompact: false, history: null,
  } });
  expect(display.ok(), await display.text()).toBe(true);

  await workbench.reload({ waitUntil: "domcontentloaded" });
  const card = workbench.locator(`[data-cell-id="${cellId}"]`).first();
  await expect(card).toBeVisible();
  // The first click focuses the cell (an unfocused cell's overlay takes it),
  // the second selects it.
  await card.click({ position: { x: 8, y: 8 } });
  await card.click({ position: { x: 8, y: 8 } });
  const show = workbench.getByRole("button", { name: "Show files" });
  if (await show.count()) await show.first().click();
  await expect(notesRow()).toBeVisible();
  // Let notes.md's editor finish booting while its tab is current. A pooled
  // sandbox frame that is parked mid-boot (the first paste opens its file in
  // this tab) can miss its host's one-shot answers and never mount when it
  // is shown again; that is a pool defect, not FP-05's, and is kept out of
  // this suite's way here.
  await notesEditorFrame();
});

test.beforeEach(async ({ browserName: _browserName }, info) => { await shell?.trace.begin(info); });
test.afterEach(async ({ browserName: _browserName }, info) => { await shell?.trace.finish(info, workbench); });

test.afterAll(async () => {
  await api?.dispose().catch(() => undefined);
  api = null;
  if (shell) {
    if (savedClipboard !== null) {
      await putClipboardText(savedClipboard).catch(() => undefined);
    }
    await shell.app.close().catch(() => undefined);
    rmSync(shell.userDataRoot, { recursive: true, force: true });
    shell = null;
  }
});

test("FP-05 shell: Ctrl+V of an OS clipboard image in the tree prompts and stores a PNG", async () => {
  await focusTree();
  await shell!.app.evaluate(async ({ clipboard, ClipboardItem }, base64) => {
    const png = new Blob([Buffer.from(base64, "base64")], { type: "image/png" });
    await clipboard.write([new ClipboardItem({ "image/png": png })]);
  }, PNG_2X2_BASE64);
  expect(await shell!.app.evaluate(({ clipboard }) => clipboard.has("image/png"))).toBe(true);

  await workbench.keyboard.press("Control+V");
  const prompt = pastePrompt("image");
  await expect(prompt, "FP-05: the Paste image prompt opens on Ctrl+V in the shell").toHaveCount(1);
  const filename = prompt.getByRole("textbox");
  await expect(filename, "FP-05: an image paste starts with its complete PNG filename").toHaveValue(/^pasted-image-\d{8}-\d{6}\.png$/);
  await expect.poll(() => filename.evaluate((input: HTMLInputElement) => [input.selectionStart, input.selectionEnd]))
    .toEqual([0, (await filename.inputValue()).length - ".png".length]);
  await workbench.keyboard.insertText("shell-shot");
  await expect(filename, "FP-05: replacing the selected stem preserves the image extension").toHaveValue("shell-shot.png");
  await filename.press("Enter");
  await expect(prompt).toHaveCount(0);

  await expect.poll(filePaths, { message: "FP-05: shell-shot.png lands in the cell root" }).toContain("shell-shot.png");
  const body = await api!.get(`/api/channels/${channel}/cells/${cellId}/files/shell-shot.png`);
  expect(body.ok(), await body.text()).toBe(true);
  const bytes = await body.body();
  expect([...bytes.subarray(0, 8)], "FP-05: the stored bytes are a PNG").toEqual(PNG_MAGIC);
});

test("FP-05 shell: Ctrl+V of OS clipboard markdown text prompts with .md and stores it", async () => {
  await focusTree();
  await putClipboardText(PASTED_MARKDOWN);
  const before = await filePaths();

  await workbench.keyboard.press("Control+V");
  const prompt = pastePrompt("text");
  await expect(prompt, "FP-05: the Paste text prompt opens on Ctrl+V in the shell").toHaveCount(1);
  const filename = prompt.getByRole("textbox");
  await expect(filename, "FP-05: markdown-looking text starts with its complete Markdown filename").toHaveValue(/^pasted-text-\d{8}-\d{6}\.md$/);
  const name = await filename.inputValue();
  await filename.press("Enter");
  await expect(prompt).toHaveCount(0);

  await expect.poll(filePaths, { message: "FP-05: pasted-text-*.md lands in the cell root" }).toContain(name);
  expect(before).not.toContain(name);
  const body = await api!.get(`/api/channels/${channel}/cells/${cellId}/files/${encodeURIComponent(name)}`);
  expect(body.ok(), await body.text()).toBe(true);
  expect(await body.text(), "FP-05: the stored text is the clipboard's").toBe(PASTED_MARKDOWN);
});

test("FP-05 shell: Paste here reads the OS clipboard through navigator.clipboard.read()", async () => {
  await putClipboardText(PASTED_MARKDOWN);
  // Right-click the tree's background (below the rows), not a row.
  const tree = workbench.locator(`[data-cell-id="${cellId}"] [data-filenode-path]`).last();
  const rowBox = (await tree.boundingBox())!;
  await workbench.mouse.click(rowBox.x + rowBox.width / 2, rowBox.y + rowBox.height + 60, { button: "right" });
  const pasteHere = workbench.getByRole("button", { name: "Paste here", exact: true });
  await expect(pasteHere, "FP-05: the tree background menu offers Paste here").toBeVisible();
  await pasteHere.click();

  const prompt = pastePrompt("text");
  await expect(prompt, "FP-05: Paste here opens the Paste text prompt from navigator.clipboard.read()").toHaveCount(1);
  await expect(workbench.getByText(KEYBOARD_HINT), "FP-05: no Clipboard API permission failure in the shell").toHaveCount(0);
  const filename = prompt.getByRole("textbox");
  await expect(filename).toHaveValue(/^pasted-text-\d{8}-\d{6}\.md$/);
  await expect.poll(() => filename.evaluate((input: HTMLInputElement) => [input.selectionStart, input.selectionEnd]))
    .toEqual([0, (await filename.inputValue()).length - ".md".length]);
  await workbench.keyboard.insertText("menu-paste");
  await expect(filename).toHaveValue("menu-paste.md");
  await filename.press("Enter");
  await expect(prompt).toHaveCount(0);
  await expect.poll(filePaths, { message: "FP-05: Paste here stores menu-paste.md" }).toContain("menu-paste.md");
});

test("FP-05 shell: Ctrl+V inside the markdown editor is the editor's, not the files pane's", async () => {
  // Each landed paste opened its file in the active tab: open notes.md again.
  await notesRow().click();
  const editor = (await notesEditorFrame()).locator(".ProseMirror");
  await expect(editor).toContainText("Shell paste fixture.");
  await editor.getByText("Shell paste fixture.").click();
  await workbench.keyboard.press("End");

  const marker = `editor-paste-${randomUUID().slice(0, 8)}`;
  await putClipboardText(` ${marker}`);
  const before = await filePaths();
  await workbench.keyboard.press("Control+V");

  await expect(editor, "FP-05: the editor takes a Ctrl+V aimed at it").toContainText(marker);
  await expect(pastePrompt(), "FP-05: no files-pane prompt opens for a paste inside an editable").toHaveCount(0);
  expect((await filePaths()).filter((path) => !before.includes(path)), "FP-05: the paste adds no file").toEqual([]);
});
