import { expect, type Page } from "@playwright/test";

/**
 * Shared helpers for the Playwright specs that drive the browser demo.
 *
 * Every request goes the way the app's own requests go: `fetch('../api/…')`
 * from the page, through the service worker into pyodide. Nothing here talks
 * to a server process — there is none.
 */

export const PLAYGROUND_READY_EXPR =
  "window.__COLLIMATE_PLAYGROUND__ && window.__COLLIMATE_PLAYGROUND__.ready";

/**
 * The seeded channel's id. The app addresses a channel by its UUID; the
 * boot info (`window.__COLLIMATE_PLAYGROUND__.channel`) carries the one the
 * demo created.
 */
export async function demoChannelId(page: Page): Promise<string> {
  const id = await page.evaluate(
    () =>
      (window as unknown as { __COLLIMATE_PLAYGROUND__?: { channel?: { id: string } | null } })
        .__COLLIMATE_PLAYGROUND__?.channel?.id ?? null,
  );
  expect(id, "the boot info should name the seeded channel").toBeTruthy();
  return id!;
}

/** The three runner identities channel.yaml seeds; discovery order may differ. */
export const DEMO_RUNNERS = [
  "terminal-session",
  "chat-assistant",
  "modify-files",
] as const;

// Register before navigation, including failures that occur before ready.
const refusalListeners = new WeakSet<Page>();
export function logVerifierRefusals(page: Page) {
  if (refusalListeners.has(page)) return;
  refusalListeners.add(page);
  page.on("console", message => {
    if (message.text().startsWith("[collimate-verifier-refusal] ")) {
      console.log(message.text());
    }
  });
}

/**
 * Waits for pyodide boot + SW activation. A cold boot downloads pyodide and
 * the micropip wheels from the CDN (~30–60 s on CI); a warm one takes a few
 * seconds. 120 s bounds readiness; an existing boot-failure overlay reports its cause promptly.
 */
export async function waitForPlaygroundReady(page: Page, timeout = 120_000) {
  logVerifierRefusals(page);
  try {
    const result = await page.waitForFunction(() => {
      const overlay = document.getElementById("collimate-playground-overlay");
      const card = overlay?.firstElementChild;
      if (card?.firstElementChild?.textContent === "The demo failed to start") {
        return { error: card.children[1]?.textContent || "Unknown boot failure" };
      }
      return (window as unknown as { __COLLIMATE_PLAYGROUND__?: { ready: boolean } })
        .__COLLIMATE_PLAYGROUND__?.ready ? { ready: true } : false;
    }, undefined, { timeout });
    const value = await result.jsonValue();
    await result.dispose();
    if (value && "error" in value) throw new Error(`Boot failed: ${value.error}`);
  } catch (error) {
    const stage = await page.evaluate(() => {
      const card = document.getElementById("collimate-playground-overlay")?.firstElementChild;
      return card?.lastElementChild?.firstElementChild?.textContent || "unavailable";
    }).catch(() => "unavailable");
    throw new Error(`${error instanceof Error ? error.message : String(error)}; last boot stage: ${stage}`);
  }
  const controlling = await page.evaluate(() => !!navigator.serviceWorker.controller);
  expect(controlling, "service worker should control the demo page").toBe(true);
}

export async function gotoPlayground(page: Page) {
  logVerifierRefusals(page);
  // Never wait for networkidle: the channel SSE stream stays open forever.
  await page.goto("/playground/", { waitUntil: "load" });
  await waitForPlaygroundReady(page);
}

/**
 * Resolves once the page has the channel's settings document
 * (`GET /channels/{channel}/settings`); start it before navigating. The app
 * fetches it after boot readiness and, when it lands, adopts the channel's
 * saved Commands pane state, theme and key bindings from it.
 */
export async function channelSettingsLoaded(page: Page): Promise<void> {
  const response = await page.waitForResponse(
    (r) => r.request().method() === "GET" && /^\/api\/channels\/[^/]+\/settings$/.test(new URL(r.url()).pathname),
    { timeout: 0 },
  );
  await response.finished();
}

export interface ApiResult<T = unknown> {
  status: number;
  body: T;
}

/** `fetch('../api' + path)` from the page; JSON bodies parsed when possible. */
export async function api<T = unknown>(
  page: Page,
  method: string,
  path: string,
  body?: unknown,
): Promise<ApiResult<T>> {
  return page.evaluate(
    async ([method, path, body]) => {
      const r = await fetch("../api" + path, {
        method,
        headers: body === undefined ? {} : { "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await r.text();
      try {
        return { status: r.status, body: JSON.parse(text) };
      } catch {
        return { status: r.status, body: text };
      }
    },
    [method, path, body] as const,
  ) as Promise<ApiResult<T>>;
}

/**
 * One new cell in a new stack, made the way the app imports files: each text
 * streams to `POST /api/blob/stream`, then `POST /channels/{channel}/import`
 * commits the cell. Answers the import's status with the new cell's id.
 */
export async function importTextCell(
  page: Page,
  name: string,
  files: Array<{ path: string; text: string }>,
): Promise<ApiResult<{ cell_id: string }>> {
  const channel = await demoChannelId(page);
  return page.evaluate(
    async ([channel, name, files]) => {
      const refs = [];
      for (const file of files) {
        const stored = await fetch("../api/blob/stream", { method: "POST", body: file.text });
        if (!stored.ok) return { status: stored.status, body: await stored.text() };
        refs.push({ path: file.path, blob_hash: (await stored.json()).blob_hash });
      }
      const r = await fetch(`../api/channels/${channel}/import`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ insert_index: null, stacks: [{ name: "", cells: [{ name, files: refs, dirs: [] }] }] }),
      });
      if (!r.ok) return { status: r.status, body: await r.text() };
      return { status: r.status, body: { cell_id: (await r.json()).cell_ids[0] } };
    },
    [channel, name, files] as const,
  ) as Promise<ApiResult<{ cell_id: string }>>;
}

export interface CellSummary {
  id: string;
  name: string;
  pinned?: string | null;
  files?: Array<{ name: string }>;
}

export interface DemoState {
  channelId: string;
  channelName: string;
  cells: CellSummary[];
}

/** The one channel and its main-stack cells, in canvas order. */
export async function demoState(page: Page): Promise<DemoState> {
  const { body } = await api<{
    channels: Array<{ id: string; name: string; main_stacks: Array<{ cells: CellSummary[] }> }>;
  }>(page, "GET", "/state");
  expect(body.channels.length, "the demo seeds exactly one channel").toBe(1);
  const ch = body.channels[0];
  return {
    channelId: ch.id,
    channelName: ch.name,
    cells: ch.main_stacks.flatMap((s) => s.cells),
  };
}

export async function cellByName(page: Page, name: string): Promise<CellSummary> {
  const { cells } = await demoState(page);
  const cell = cells.find((c) => c.name === name);
  expect(cell, `cell '${name}' should exist; have ${cells.map((c) => c.name).join(", ")}`).toBeTruthy();
  return cell!;
}

export async function cellById(page: Page, id: string): Promise<CellSummary> {
  const { cells } = await demoState(page);
  const cell = cells.find((c) => c.id === id);
  expect(cell, `cell ${id} should still exist`).toBeTruthy();
  return cell!;
}

/** The file names of the cell's current version, as the Files pane lists them. */
export async function cellFiles(page: Page, cellId: string): Promise<string[]> {
  const cell = await cellById(page, cellId);
  return (cell.files ?? []).map((f) => f.name);
}

export async function cellFileText(page: Page, cellId: string, path: string): Promise<string> {
  const { status, body } = await api(
    page,
    "GET",
    `/channels/${await demoChannelId(page)}/cells/${cellId}/files/${encodeURIComponent(path)}`,
  );
  expect(status, `${path} should be readable`).toBe(200);
  return typeof body === "string" ? body : JSON.stringify(body);
}

/**
 * The run phase (spinning_up → running → awaiting_input → …) is announced
 * only on the channel SSE stream (`run_phase {run_id, phase}`), so record it
 * in-page and poll the record.
 */
export async function trackPhases(page: Page) {
  await page.evaluate((channel) => {
    const w = window as unknown as { __phases: Record<string, string> };
    w.__phases = {};
    const es = new EventSource(`../api/channels/${channel}/state/stream`);
    es.onmessage = (e) => {
      const d = JSON.parse(e.data);
      if (d.type === "run_phase") w.__phases[d.run_id] = d.phase;
    };
  }, await demoChannelId(page));
}

export async function waitForPhase(page: Page, runId: string, phase: string, timeout = 60_000) {
  await page.waitForFunction(
    ([id, want]) => (window as unknown as { __phases?: Record<string, string> }).__phases?.[id] === want,
    [runId, phase] as const,
    { timeout },
  );
}

export interface RunRecord {
  run_id: string;
  status: string;
  failure_reason?: string | null;
}

export async function runRecord(page: Page, runId: string): Promise<RunRecord> {
  const channel = await demoChannelId(page);
  const { body } = await api<{ run: RunRecord }>(page, "GET", `/channels/${channel}/runs/${runId}`);
  return body.run;
}

/** A `/runs/active` row: the run, the cell it runs on, and its folder. */
export interface ActiveRun {
  run_id: string;
  cell_id: string;
  runner_id: string;
  runner_name: string;
  label: string;
  /** `_runs/<label>` — every file in it is the run's locked UI file. */
  folder: string;
  generator_name: string;
  phase: string;
}

export async function activeRuns(page: Page): Promise<ActiveRun[]> {
  const channel = await demoChannelId(page);
  const { status, body } = await api<ActiveRun[]>(page, "GET", `/channels/${channel}/runs/active`);
  expect(status, `active runs: ${JSON.stringify(body).slice(0, 200)}`).toBe(200);
  return body;
}

/** The `log` artifacts of a run (a finished run's swept folder lands here). */
export async function runLogPaths(page: Page, runId: string): Promise<string[]> {
  const { body } = await api<{ artifacts: Array<{ category: string; path: string }> }>(
    page,
    "GET",
    `/channels/${await demoChannelId(page)}/runs/${runId}/artifacts`,
  );
  return body.artifacts.filter((a) => a.category === "log").map((a) => a.path);
}

/** Whether any of the cell's files lies in a run folder. */
export function hasRunFiles(files: string[]): boolean {
  return files.some((f) => f.startsWith("_runs/"));
}

/** Polls the run record until it leaves `running`; returns the final record. */
export async function waitForRunEnd(page: Page, runId: string, timeout = 90_000): Promise<RunRecord> {
  const started = Date.now();
  let last: RunRecord | undefined;
  while (Date.now() - started < timeout) {
    last = await runRecord(page, runId);
    if (last.status !== "running") return last;
    await page.waitForTimeout(400);
  }
  throw new Error(`run ${runId} still running after ${timeout} ms: ${JSON.stringify(last)}`);
}

/**
 * Starts `runnerName` on `cellId` the way a visitor does: select the cell,
 * the Run shortcut (Alt+R, on the selected cell) opens the runners panel
 * inside the cell, the search box filters, Enter runs. Returns the new
 * run's `/runs/active` row.
 */
export async function runFromPanel(page: Page, cellId: string, runnerName: string): Promise<ActiveRun> {
  const before = new Set((await activeRuns(page)).map((r) => r.run_id));
  const cell = page.locator(`[data-cell-id="${cellId}"]`).first();
  const search = page.getByRole("combobox", { name: "Search runners" });
  // The shortcut acts on the selected cell: select it (a pointerdown
  // anywhere on the card does, StackItem), then retry until the runners
  // panel is open.
  for (let attempt = 0; attempt < 6 && (await search.count()) === 0; attempt++) {
    await cell.locator("[data-stack-card]").first().dispatchEvent("pointerdown", { button: 0 });
    await page.keyboard.press("Alt+r");
    await page.waitForTimeout(500);
  }
  await search.waitFor({ timeout: 10_000 });
  await search.fill(runnerName);
  await expect(page.getByRole("listbox", { name: "Runners" }).getByRole("option").first()).toContainText(runnerName);
  await search.press("Enter");
  let run: ActiveRun | undefined;
  for (let i = 0; i < 40 && !run; i++) {
    await page.waitForTimeout(250);
    run = (await activeRuns(page)).find((r) => r.cell_id === cellId && !before.has(r.run_id));
  }
  expect(run, `a run of '${runnerName}' should be active on cell ${cellId}`).toBeTruthy();
  expect(run!.folder).toBe(`_runs/${run!.label}`);
  return run!;
}

/**
 * Starts a runner on a cell through the app's own run route — what a second
 * pick in the runners panel sends — and returns the POST answer's run fields.
 */
export async function startRun(page: Page, cellId: string, runnerId: string): Promise<ActiveRun> {
  const channel = await demoChannelId(page);
  const { status, body } = await api<ActiveRun & { status: string }>(page, "POST", `/channels/${channel}/runs`, {
    runner: runnerId,
    cell_id: cellId,
  });
  expect(status, `start ${runnerId}: ${JSON.stringify(body).slice(0, 200)}`).toBe(200);
  expect(body.cell_id).toBe(cellId);
  return body;
}

/** Clicks the click-to-focus overlay of an unfocused cell when there is one. */
export async function focusCell(page: Page) {
  const overlay = page.locator('div[title="Click to focus"]');
  if (await overlay.count()) {
    await overlay.first().click();
    await page.waitForTimeout(300);
  }
}

/**
 * Edits a saved runner's main-config values through the runner editor API —
 * the same path the runner editor UI takes. The specs use it to shorten the
 * two open-ended demo windows (the terminal's keep-open, the chat's idle
 * timeout) without changing the shipped defaults.
 */
export async function setRunnerSettings(page: Page, runnerId: string, patch: Record<string, unknown>) {
  const editor = await api<{
    runner: { settings: Record<string, Record<string, unknown>> } & Record<string, unknown>;
    files: unknown[];
    revision: string;
  }>(page, "GET", `/runners/${runnerId}`);
  expect(editor.status).toBe(200);
  const main = "settings.set_yaml";
  const settings = {
    ...editor.body.runner.settings,
    [main]: { ...editor.body.runner.settings[main], ...patch },
  };
  const saved = await api(page, "PUT", `/runners/${runnerId}`, {
    runner: { ...editor.body.runner, settings },
    files: editor.body.files,
    revision: editor.body.revision,
  });
  expect(saved.status, `runner ${runnerId} update: ${JSON.stringify(saved.body).slice(0, 200)}`).toBe(200);
}

/** Everything the demo terminal wrote (its replay ring), across sessions. */
export async function terminalOutput(page: Page): Promise<string> {
  return page.evaluate(() =>
    (window as unknown as { __COLLIMATE_PLAYGROUND__: { pyodide: { runPython: (s: string) => string } } })
      .__COLLIMATE_PLAYGROUND__.pyodide.runPython(
        "from collimate_playground import terminal as t; ''.join(bytes(x._ring).decode('utf-8','replace') for x in t._sessions.values())",
      ),
  );
}

export async function fetchRendererSource(page: Page, rendererId: string) {
  return page.evaluate(async (id) => {
    const catalogResponse = await fetch("../api/renderers");
    if (!catalogResponse.ok) throw new Error(`renderer catalog: HTTP ${catalogResponse.status}`);
    const catalog = await catalogResponse.json() as Array<{ id: string; content_hash: string }>;
    const selected = catalog.find((entry) => entry.id === id);
    if (!selected) throw new Error(`renderer ${id} is not in the current catalog`);
    const r = await fetch(`../api/renderers/${encodeURIComponent(id)}/source?v=${encodeURIComponent(selected.content_hash)}`);
    return {
      sourceHash: selected.content_hash,
      etag: r.headers.get("ETag"),
      status: r.status,
      entrypoint: r.headers.get("X-Entrypoint"),
      bodyLen: (await r.text()).length,
    };
  }, rendererId);
}

/**
 * Reproduces NativeRenderer.tsx: fetch source → rewrite `/api/sdk/*` imports
 * against the origin-absolute api base → Blob → dynamic import. The current
 * source route serves canonical compiled ESM (module.js), not authored TSX.
 */
export async function loadNativeRenderer(page: Page, rendererId: string) {
  return page.evaluate(async (id) => {
    const catalogResponse = await fetch("../api/renderers");
    if (!catalogResponse.ok) return { ok: false, stage: "catalog", status: catalogResponse.status };
    const catalog = await catalogResponse.json() as Array<{ id: string; content_hash: string }>;
    const selected = catalog.find((entry) => entry.id === id);
    if (!selected) return { ok: false, stage: "catalog", error: `renderer ${id} is unavailable` };
    const res = await fetch(`../api/renderers/${encodeURIComponent(id)}/source?v=${encodeURIComponent(selected.content_hash)}`);
    if (!res.ok) return { ok: false, stage: "fetch", status: res.status };
    if (res.headers.get("X-Entrypoint") !== "module.js"
      || res.headers.get("ETag") !== `"${selected.content_hash}"`) {
      return { ok: false, stage: "identity", error: "compiled renderer identity mismatch" };
    }
    let src = await res.text();

    const apiBase = "..";
    const fullApiBase = new URL(apiBase + "/", window.location.href).toString().replace(/\/$/, "");
    src = src.replace(/['"]\/api\/sdk\/(.*?)['"]/g, `"${fullApiBase}/api/sdk/$1"`);

    try {
      const blob = new Blob([src], { type: "application/javascript" });
      const url = URL.createObjectURL(blob);
      try {
        const mod = (await import(/* @vite-ignore */ url)) as { default?: { mount?: unknown }; mount?: unknown };
        const def = mod.default || mod;
        return {
          ok: true,
          hasMount: typeof (def as { mount?: unknown }).mount === "function",
          keys: Object.keys(def || {}),
        };
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch (e) {
      return { ok: false, stage: "import", error: String((e as Error)?.stack || (e as Error)?.message || e) };
    }
  }, rendererId);
}

/**
 * The rewrite form that once shipped — `location.origin + '..'` →
 * `http://host:port..`. Kept as a signature: it must keep failing.
 */
export async function brokenRewriteStillFails(page: Page) {
  return page.evaluate(async () => {
    const fullApiBase = window.location.origin + "..";
    const src = `import { x } from "${fullApiBase}/api/sdk/react.js";\nexport default 1;`;
    const blob = new Blob([src], { type: "application/javascript" });
    const url = URL.createObjectURL(blob);
    try {
      await import(/* @vite-ignore */ url);
      return { fails: false as const };
    } catch (e) {
      return { fails: true as const, error: String(e) };
    } finally {
      URL.revokeObjectURL(url);
    }
  });
}

export interface RendererSummary {
  id: string;
  name: string;
  version: string;
  extensions: string[];
  supports_diff: boolean;
  embed: "native" | "iframe";
  content_hash: string;
}

export async function listRenderers(page: Page): Promise<RendererSummary[]> {
  return page.evaluate(async () => {
    const r = await fetch("../api/renderers");
    if (!r.ok) throw new Error(`/api/renderers returned ${r.status}`);
    return r.json();
  });
}
