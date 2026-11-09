import { existsSync, readdirSync, readFileSync, realpathSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import {
  launchShell,
  missingPrerequisite,
  packagedExecutable,
  repoRoot,
  serverIsUp,
  workbenchWindow,
  type LaunchedShell, removeTestRoot } from "./desktop";

/**
 * A built package, as it is installed: its own binary, its build stamp and
 * the server environment it carries (whose uv starts its generator runs, on
 * its Python: the first uv on the PATH it is given is a trap), with no
 * checkout and no COLLIMATE_SERVER_SPEC behind it. Runs only with
 * `COLLIMATE_E2E_PACKAGED=<the package's executable>` (desktop.ts), which
 * release.yml sets on every leg that carries a server; a developer runs it
 * against `bash build/build_desktop.sh`'s output:
 *
 *   COLLIMATE_E2E_PACKAGED=$PWD/../desktop/release/linux-unpacked/collimate-desktop \
 *     xvfb-run -a npx playwright test -c playwright.electron.config.ts packaged.spec.ts
 *
 * The dependency probe's run fetches pydantic on its first run, so with no
 * network it needs a uv cache warmed beforehand, named by
 * `COLLIMATE_E2E_UV_CACHE` (desktop.ts):
 *
 *   UV_CACHE_DIR=$CACHE <resources>/server/bin/uv sync --script \
 *     build/dependency_probe/main.py --python <resources>/server/bin/python3
 */

const blocked = packagedExecutable ? missingPrerequisite() : "COLLIMATE_E2E_PACKAGED names no package";
test.skip(blocked !== null, `packaged-shape e2e: ${blocked ?? ""}`);

test.describe.configure({ mode: "serial" });

interface BuildInfo {
  version: string;
  commit: string | null;
  dirty: boolean | null;
  builtAt: string | null;
  serverEnvironmentHash: string | null;
}

type WindowWithBridge = Window & {
  collimateDesktop?: { buildInfo(): BuildInfo; server: { id: string; url: string } };
};

let shell: LaunchedShell | null = null;
let workbench: Page;
let serverUrl = "";

test.beforeAll(async ({ browserName: _browserName }, info) => {
  shell = await launchShell();
  try {
    workbench = await workbenchWindow(shell.app);
    serverUrl = new URL(workbench.url()).origin;
  } catch (error) {
    await shell.trace.finish(info, shell.app.windows()[0], true);
    throw error;
  }
});

test.beforeEach(async ({ browserName: _browserName }, info) => { await shell?.trace.begin(info); });
test.afterEach(async ({ browserName: _browserName }, info) => { await shell?.trace.finish(info, workbench); });

test.afterAll(async () => {
  if (shell) {
    await shell.app.close().catch(() => undefined);
    removeTestRoot(shell.userDataRoot);
    shell = null;
  }
});

test("the installed app starts the server environment it carries, looking for no uv, and names its build", async () => {
  expect(workbench.url()).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/$/);
  expect(await serverIsUp(serverUrl)).toBe(true);

  const build = await workbench.evaluate(() => {
    const desktop = (window as WindowWithBridge).collimateDesktop;
    if (!desktop) throw new Error("window.collimateDesktop is not there");
    return desktop.buildInfo();
  });
  expect(build.version).not.toBe("0.0.0-dev");
  expect(build.commit).toMatch(/^[0-9a-f]{40}$/);
  expect(typeof build.dirty).toBe("boolean");
  expect(Number.isNaN(Date.parse(build.builtAt ?? ""))).toBe(false);
  expect(build.serverEnvironmentHash).toMatch(/^[0-9a-f]{64}$/);

  // The installed profile, and the spawn the shell recorded: the carried
  // interpreter (or, in an AppImage, its copy named by the environment's
  // hash), isolated, the environment's hash part of the identity.
  expect(shell!.runtimeDir.endsWith(`${path.sep}Collimate`)).toBe(true);
  const paths = await shell!.app.evaluate(({ app }) => ({
    userData: app.getPath("userData"), appData: app.getPath("appData"), home: app.getPath("home"), envHome: process.env["HOME"] ?? null,
  }));
  expect(paths, "the shell keeps its files in the harness's directory").toMatchObject({ userData: shell!.runtimeDir });
  const record = JSON.parse(readFileSync(path.join(shell!.runtimeDir, "server.json"), "utf8")) as { spec: string };
  const hash = build.serverEnvironmentHash!;
  const python = record.spec.split(" ")[0]!;
  expect(record.spec).toContain(" -I -B -m collimate_server --port ");
  expect(record.spec.endsWith(` @ server ${hash}`)).toBe(true);
  const copy = path.join(shell!.runtimeDir, "server-environments", hash.slice(0, 16), "bin", "python3");
  expect([copy, expect.stringMatching(/[/\\][Rr]esources[/\\]server[/\\]bin[/\\]python3$/)]).toContainEqual(python); // Contents/Resources on macOS
  expect(existsSync(python)).toBe(true);

  const log = readFileSync(path.join(shell!.runtimeDir, "logs", "server.log"), "utf8");
  expect(log).toContain(`build ${build.commit!.slice(0, 10)}`);
  expect(log).toContain(`exec: ${python} -I -B -m collimate_server`);
  expect(log).not.toMatch(/using uv:|looked for uv|uv run|uvx/);
});

test("the workbench is the one the carried server serves", async ({ browserName: _browserName }, info) => {
  const health = await (await fetch(`${serverUrl}/health`)).json() as { version: string; bridge: number };
  const build = await workbench.evaluate(() => (window as WindowWithBridge).collimateDesktop!.buildInfo());
  expect(health.version).toBe(build.version);
  const ready = await (await fetch(`${serverUrl}/ready`)).json() as { status: string; checks: Record<string, unknown> };
  expect(ready.status).toBe("ok");
  expect(ready.checks["bundled"]).toBe("ok");
  // The page itself, from the carried UI: the app shell has mounted.
  await expect(workbench.locator("#root")).not.toBeEmpty({ timeout: 30_000 });
  await info.attach("workbench", { body: await workbench.screenshot(), contentType: "image/png" });
});

// The probes (build/run_probe/, build/dependency_probe/): generators that
// write which uv started them, the Python they ran on and what their
// PYTHONPATH holds; the dependency probe also where its pydantic came from
// (its header pins one other than the server environment's).
const PROBES = {
  run: { folder: "run_probe", output: "ran-with.json" },
  dependency: { folder: "dependency_probe", output: "own-deps.json" },
} as const;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;

async function call(method: string, route: string, body?: unknown, raw?: Buffer): Promise<Json> {
  const response = await fetch(`${serverUrl}${route}`, {
    method,
    headers: raw ? { "Content-Type": "application/octet-stream" } : body === undefined ? undefined : { "Content-Type": "application/json" },
    body: raw ? new Uint8Array(raw) : body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  expect(response.status, `${method} ${route}: ${text}`).toBeLessThan(300);
  return text ? JSON.parse(text) : null;
}

/** The environment the recorded spawn runs from (its `bin/python3`'s root). */
function serverRoot(): string {
  const record = JSON.parse(readFileSync(path.join(shell!.runtimeDir, "server.json"), "utf8")) as { spec: string };
  return path.dirname(path.dirname(record.spec.split(" ")[0]!));
}

/**
 * As the app does it: a channel, the probe's cell imported and its package
 * added to Capabilities, a runner saved from its default config, a run to
 * its end; then what the run wrote and its host log lines.
 */
async function runProbe(probe: { folder: string; output: string }, info: Json): Promise<{ said: Json; hostLog: string[] }> {
  const dir = path.join(repoRoot, "build", probe.folder);
  const name = probe.folder.replace("_", " ");
  const channel = (await call("POST", `/api/channels?name=${encodeURIComponent(`${name} ${Date.now()}`)}`, {})).id as string;
  const files = [];
  for (const file of readdirSync(dir, { withFileTypes: true }).filter((e) => e.isFile()).map((e) => e.name).sort()) {
    const { blob_hash } = await call("POST", `/api/channels/${channel}/blob/stream`, undefined, readFileSync(path.join(dir, file)));
    files.push({ path: `${probe.folder}/${file}`, blob_hash });
  }
  const notes = (await call("POST", `/api/channels/${channel}/blob/stream`, undefined, Buffer.from("# Probe runs\n"))).blob_hash as string;
  const imported = await call("POST", `/api/channels/${channel}/import`, { insert_index: null, stacks: [
    { name, cells: [{ name, files, dirs: [] }] },
    { name: "Probe runs", cells: [{ name: "Probe runs", files: [{ path: "notes.md", blob_hash: notes }], dirs: [] }] },
  ] });
  const [sourceCell, targetCell] = imported.cell_ids as string[];
  const request = { source_cell_id: sourceCell, definition_path: `${probe.folder}/${probe.folder}.generator`, target_cell_id: null };
  const preview = await call("POST", `/api/channels/${channel}/packages/preview`, request);
  await call("POST", `/api/channels/${channel}/packages/add`, {
    ...request, expected_content_hash: preview.content_hash, expected_target_revision: preview.target_revision,
  });
  const generator = preview.id as string;
  const draft = await call("GET", `/api/runner_generators/${generator}/configs/default`);
  await call("POST", "/api/runners", { runner: { ...draft.runner, generator, id: generator, name }, files: draft.files });
  const runId = (await call("POST", `/api/channels/${channel}/runs`, { runner: generator, cell_id: targetCell })).run_id as string;

  let events: Json = null;
  await expect.poll(async () => {
    events = await call("GET", `/api/channels/${channel}/runs/${runId}/events_since?since_id=0`);
    return events.is_terminal as boolean;
  }, { timeout: 240_000, intervals: [500, 1_000, 2_000], message: `the ${probe.folder} run to finish` }).toBe(true);
  const rows = events.events as Json[];
  const output = rows.filter((e) => ["stdout", "stderr", "log"].includes(e.type)).map((e) => e.payload).join("");
  await info.attach(`${probe.folder} run`, { body: JSON.stringify(events, null, 2), contentType: "application/json" });
  expect(events.status, output).toBe("completed");

  const paths = (await call("GET", `/api/channels/${channel}/cells/${targetCell}/file-paths`)).paths as string[];
  const written = paths.filter((p) => p.split("/").pop() === probe.output);
  expect(written).toHaveLength(1);
  const said = await call("GET", `/api/channels/${channel}/cells/${targetCell}/files/${written[0]!.split("/").map(encodeURIComponent).join("/")}`);
  return { said, hostLog: rows.filter((e) => e.type === "log").map((e) => String(e.payload)) };
}

/** Started by the carried uv, on the carried Python (and its log says so), with the SDK alone on its path. */
function expectTheCarriedTools(said: Json, hostLog: string[], root: string): void {
  const uv = path.join(root, "bin", "uv");
  expect(realpathSync(said.uv as string)).toBe(realpathSync(uv));
  expect(said.python_home).toBe(realpathSync(root));
  expect(hostLog).toContain(`Python ${said.python as string}, the server environment's (${path.join(root, "bin", "python3")})`);
  expect(said.pythonpath).toHaveLength(1);
  expect(said.pythonpath[0].holds).toEqual(["collimate", "gen"]);
  expect(realpathSync(said.pythonpath[0].path as string).startsWith(realpathSync(root) + path.sep)).toBe(true);
}

test("a generator run starts with the uv the package carries, on its Python, seeing only the SDK", async ({ browserName: _browserName }, info) => {
  test.setTimeout(300_000);
  const root = serverRoot();
  const record = JSON.parse(readFileSync(path.join(shell!.runtimeDir, "server.json"), "utf8")) as { spec: string };
  expect(record.spec).toContain(` --server-env ${root} @ server `);
  expect(existsSync(path.join(root, "bin", "uv"))).toBe(true);

  const { said, hostLog } = await runProbe(PROBES.run, info);
  expectTheCarriedTools(said, hostLog, root);

  // The probe's channel, active since it was made, in the workbench.
  await workbench.reload({ waitUntil: "domcontentloaded" });
  await expect(workbench.locator("#root")).not.toBeEmpty({ timeout: 30_000 });
  const shot = info.outputPath("probe-channel.png");
  await workbench.screenshot({ path: shot });
  await info.attach("probe channel", { path: shot, contentType: "image/png" });
});

test("a generator run imports its own dependencies, not the server's, on the carried Python", async ({ browserName: _browserName }, info) => {
  test.setTimeout(300_000);
  const root = serverRoot();
  const { said, hostLog } = await runProbe(PROBES.dependency, info);
  expectTheCarriedTools(said, hostLog, root);
  const pin = /"pydantic==([^"]+)"/.exec(readFileSync(path.join(repoRoot, "build", "dependency_probe", "main.py"), "utf8"))![1];
  expect(said.pydantic.version).toBe(pin);
  const carried = readdirSync(path.join(root, "lib"), { recursive: true, encoding: "utf8" })
    .filter((entry) => /site-packages[/\\]pydantic-[^/\\]+\.dist-info$/.test(entry));
  expect(carried, "the server environment carries a pydantic of its own").toHaveLength(1);
  expect(carried[0]).not.toContain(`pydantic-${pin}.dist-info`);
  for (const module of ["pydantic", "pydantic_core"]) {
    expect(realpathSync(said[module].file as string).startsWith(realpathSync(root) + path.sep), `${module}: ${said[module].file}`).toBe(false);
  }
});

test("quitting takes the carried server down with the app", async ({ browserName: _browserName }, info) => {
  await shell!.trace.beforeClose(info, workbench);
  await shell!.app.close();
  await expect.poll(() => serverIsUp(serverUrl), { timeout: 30_000 }).toBe(false);
});
