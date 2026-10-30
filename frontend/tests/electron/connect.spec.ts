import { spawn, type ChildProcess } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, openSync, closeSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import {
  apiProject, isolatedEnvironment, launchShell, missingPrerequisite, repoRoot,
  serverIsUp, testRoot, workbenchWindow, type LaunchedShell,
} from "./desktop";

test.skip(missingPrerequisite() !== null, "desktop e2e prerequisites are missing");
test.describe.configure({ mode: "serial" });

/**
 * The servers registry against real APIs: two tokened remotes (each its
 * own data dir and token) plus the local server the shell always starts.
 * One window shows one server; picking a channel on the other remote
 * recreates the window on that origin with only that origin's token, and
 * the local server keeps running throughout.
 */

interface RequestRecord { path: string; query: string; authorization: string }
interface Remote { root: string; token: string; url: string; child: ChildProcess }
interface Entry {
  id: string; kind: string; name: string; url: string; status: string;
  hasToken: boolean; channels: { id: string; name: string }[] | null;
  activeChannelId: string | null; isWindowServer: boolean;
}
/** The bits of `window.collimateDesktop` this suite drives. */
interface Bridge {
  bridge: number;
  server: Record<string, unknown>;
  servers: {
    list(): Promise<Entry[]>;
    add(draft: { url: string; token?: string; name?: string }): Promise<Entry>;
    probe(candidate: { url: string; token?: string }): Promise<{ ok: boolean; message?: string }>;
    openChannel(id: string, channelId: string): Promise<void>;
  };
}
/** Not `Window & …`: the app's own global declaration would merge into it. */
type WindowWithBridge = { collimateDesktop: Bridge };
/** The bits of a main-process BrowserWindow this suite reads. */
interface WindowLike {
  getMinimumSize(): number[];
  getBounds(): { x: number; y: number; width: number; height: number };
  setBounds(bounds: { x: number; y: number; width: number; height: number }): void;
}

const remotes: Remote[] = [];
let shell: LaunchedShell | undefined;
let workbench: Page;

// Every request reaches the real API; the wrapper only records what the
// shell sent (path, query nonce, Authorization). The bound socket is passed
// into uvicorn, avoiding a reserve/close port race.
const REMOTE = `
import json, pathlib, socket, sys
sys.path.insert(0, sys.argv[1])
from main import app
import uvicorn
async def observed(scope, receive, send):
    if scope['type'] == 'http':
        headers = dict(scope['headers'])
        with open('requests.jsonl', 'a') as stream:
            stream.write(json.dumps({'path': scope['path'], 'query': scope['query_string'].decode(),
                'authorization': headers.get(b'authorization', b'').decode()}) + '\\n')
    await app(scope, receive, send)
sock = socket.socket()
sock.bind(('127.0.0.1', 0))
sock.listen(128)
pathlib.Path('port').write_text(str(sock.getsockname()[1]))
uvicorn.Server(uvicorn.Config(observed, log_level='warning')).run(sockets=[sock])
`;

async function startRemote(): Promise<Remote> {
  const root = testRoot();
  const token = `electron-test-${randomUUID()}`;
  const log = openSync(path.join(root, "server.log"), "w");
  const child = spawn(path.join(repoRoot, ".venv", "bin", "python"), ["-c", REMOTE, apiProject], {
    cwd: root, env: { ...isolatedEnvironment(root), COLLIMATE_TOKEN: token }, stdio: ["ignore", log, log],
  });
  closeSync(log);
  const remote: Remote = { root, token, child, url: "" };
  remotes.push(remote); // teardown also owns partial startup failures
  let spawnError: Error | undefined;
  child.on("error", (error) => { spawnError = error; });
  await expect.poll(async () => {
    if (spawnError) throw spawnError;
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(`remote API exited: ${readFileSync(path.join(root, "server.log"), "utf8")}`);
    }
    const portFile = path.join(root, "port");
    if (!existsSync(portFile)) return false;
    remote.url = `http://127.0.0.1:${readFileSync(portFile, "utf8")}`;
    return serverIsUp(remote.url);
  }, { timeout: 60_000 }).toBe(true);
  expect((await fetch(`${remote.url}/api/state`)).status).toBe(401);
  expect((await fetch(`${remote.url}/api/state`, { headers: { Authorization: `Bearer ${token}` } })).status).toBe(200);
  return remote;
}

/** Create a channel on a remote with its own token; returns its id. */
async function createChannel(remote: Remote, name: string): Promise<string> {
  const response = await fetch(`${remote.url}/api/channels?name=${encodeURIComponent(name)}`, {
    method: "POST", headers: { Authorization: `Bearer ${remote.token}` },
  });
  expect(response.ok).toBe(true);
  const catalog = await (await fetch(`${remote.url}/api/channels`, {
    headers: { Authorization: `Bearer ${remote.token}` },
  })).json() as { channels: { id: string; name: string }[] };
  const channel = catalog.channels.find((candidate) => candidate.name === name);
  if (!channel) throw new Error(`channel ${name} was not created on ${remote.url}`);
  return channel.id;
}

function recorded(remote: Remote, nonce: string): RequestRecord[] {
  const file = path.join(remote.root, "requests.jsonl");
  return readFileSync(file, "utf8").trim().split("\n").filter(Boolean)
    .map((line) => JSON.parse(line) as RequestRecord).filter((row) => row.query === nonce);
}

async function privateAccess(page: Page, remote: Remote): Promise<void> {
  const nonce = randomUUID();
  expect(await page.evaluate(async (query) => (await fetch(`/api/state?${query}`)).status, nonce)).toBe(200);
  expect(recorded(remote, nonce)).toEqual([{ path: "/api/state", query: nonce, authorization: `Bearer ${remote.token}` }]);
}

async function noCredentialFor(remote: Remote): Promise<void> {
  const nonce = randomUUID();
  // The actual default Electron session is the owner of origin-specific headers.
  const status = await shell!.app.evaluate(async ({ session }, url) => {
    const response = await session.defaultSession.fetch(url);
    return response.status;
  }, `${remote.url}/api/state?${nonce}`);
  expect(status).toBe(401);
  expect(recorded(remote, nonce)).toEqual([{ path: "/api/state", query: nonce, authorization: "" }]);
}

async function list(page: Page): Promise<Entry[]> {
  return page.evaluate(() => (window as unknown as WindowWithBridge).collimateDesktop.servers.list());
}

test.beforeAll(async ({ browserName: _browserName }, info) => {
  const first = await startRemote();
  await startRemote();
  shell = await launchShell({ connect: { url: first.url, token: first.token } });
  try {
    workbench = await workbenchWindow(shell.app, 60_000, first.url);
  } catch (error) {
    await shell.trace.finish(info, shell.app.windows()[0], true);
    throw error;
  }
});
test.beforeEach(async ({ browserName: _browserName }, info) => { await shell?.trace.begin(info); });
test.afterEach(async ({ browserName: _browserName }, info) => { await shell?.trace.finish(info, workbench); });
test.afterAll(async () => {
  try {
    await shell?.app.close().catch(() => undefined);
  } finally {
    if (shell) rmSync(shell.userDataRoot, { recursive: true, force: true });
    for (const remote of remotes) {
      if (remote.child.pid && remote.child.exitCode === null && remote.child.signalCode === null) {
        const exited = new Promise<void>((resolve) => remote.child.once("exit", () => resolve()));
        remote.child.kill("SIGTERM");
        const timer = setTimeout(() => remote.child.kill("SIGKILL"), 10_000);
        await exited;
        clearTimeout(timer);
      }
      rmSync(remote.root, { recursive: true, force: true });
    }
  }
});

test("opens the automation remote with its token, exposes no token and forwards it nowhere else __P0F32_F1015", async () => {
  const first = remotes[0]!;
  const info = await workbench.evaluate(() => (window as unknown as WindowWithBridge).collimateDesktop.server);
  expect(info).toMatchObject({ kind: "remote", url: first.url, dataDir: null });
  expect(info).not.toHaveProperty("mode");
  expect(JSON.stringify(info)).not.toContain(first.token);
  await privateAccess(workbench, first);
  await noCredentialFor(remotes[1]!);
  const settings = path.join(shell!.runtimeDir, "settings.json");
  expect(existsSync(settings) ? readFileSync(settings, "utf8") : "").not.toContain(first.token);
});

test("the registry adds a second real API and its catalog lists both remotes and the local server (MS-02)", async () => {
  const [first, second] = [remotes[0]!, remotes[1]!];
  const pickedName = `picked-${randomUUID().slice(0, 8)}`;
  await createChannel(second, pickedName);

  // A tokenless probe of a tokened server is a sentence, not a save.
  const refused: { ok: boolean; message?: string } = await workbench.evaluate((url) =>
    (window as unknown as WindowWithBridge).collimateDesktop.servers.probe({ url }), second.url);
  expect(refused).toMatchObject({ ok: false });
  expect(refused.message).toMatch(/token/i);

  // The token is typed in the page once and never comes back.
  const added = await workbench.evaluate(({ url, token }) =>
    (window as unknown as WindowWithBridge).collimateDesktop.servers.add({ url, token, name: "Second" }),
  { url: second.url, token: second.token });
  expect(added).toMatchObject({ kind: "remote", name: "Second", url: second.url, status: "running", hasToken: true });
  expect(JSON.stringify(added)).not.toContain(second.token);
  expect(added.channels?.map((channel) => channel.name)).toContain(pickedName);

  // The local server starts at every launch, whatever the window shows.
  await expect.poll(async () => (await list(workbench)).find((entry) => entry.id === "local")?.status, {
    timeout: 180_000,
  }).toBe("running");
  const entries = await list(workbench);
  expect(entries.map((entry) => [entry.id === "local" ? "local" : entry.url, entry.isWindowServer])).toEqual([
    ["local", false],
    [first.url, true],
    [second.url, false],
  ]);
  for (const entry of entries) expect(entry.status).toBe("running");
  expect(JSON.stringify(entries)).not.toContain(first.token);
  expect(JSON.stringify(entries)).not.toContain(second.token);
  // The catalog polls carried each server its own token, explicitly.
  const polls = readFileSync(path.join(second.root, "requests.jsonl"), "utf8").trim().split("\n")
    .map((line) => JSON.parse(line) as RequestRecord).filter((row) => row.path === "/api/channels");
  expect(polls.length).toBeGreaterThan(0);
  for (const poll of polls.filter((row) => row.authorization)) {
    expect(poll.authorization).toBe(`Bearer ${second.token}`);
  }
  const settings = readFileSync(path.join(shell!.runtimeDir, "settings.json"), "utf8");
  expect(settings).toContain(second.token); // stored by the shell, 0600
  expect(settings).not.toContain(first.token); // the automation token never is
});

test("picking a channel on the other server recreates the window there with only that origin's token; local stays up (MS-01)", async () => {
  const [first, second] = [remotes[0]!, remotes[1]!];
  const entries = await list(workbench);
  const target = entries.find((entry) => entry.url === second.url)!;
  const local = entries.find((entry) => entry.id === "local")!;
  const channel = target.channels!.find((candidate) => candidate.name.startsWith("picked-"))!;

  // The old window's geometry once it has settled (a tiling compositor
  // places windows itself). The shell asks for the same geometry for the
  // new window; its log says what it asked for.
  const bounds = () => shell!.app.evaluate(({ BrowserWindow }) =>
    (BrowserWindow.getAllWindows() as WindowLike[]).find((candidate) => candidate.getMinimumSize()[0] === 900)!.getBounds());
  let before = await bounds();
  await expect.poll(async () => {
    const now = await bounds();
    const settled = JSON.stringify(now) === JSON.stringify(before);
    before = now;
    return settled;
  }, { intervals: [500] }).toBe(true);

  const previous = workbench;
  // Not awaited: the window that asked is the one being replaced.
  await workbench.evaluate(({ id, channelId }) => {
    void (window as unknown as WindowWithBridge).collimateDesktop.servers.openChannel(id, channelId);
  }, { id: target.id, channelId: channel.id });
  workbench = await workbenchWindow(shell!.app, 60_000, second.url);
  await expect.poll(() => previous.isClosed()).toBe(true);

  const info = await workbench.evaluate(() => (window as unknown as WindowWithBridge).collimateDesktop.server);
  expect(info).toMatchObject({ id: target.id, kind: "remote", name: "Second", url: second.url });
  await privateAccess(workbench, second);
  await noCredentialFor(first);
  // Selected on that server, with that server's token.
  const catalog = await (await fetch(`${second.url}/api/channels`, {
    headers: { Authorization: `Bearer ${second.token}` },
  })).json() as { active_channel_id: string };
  expect(catalog.active_channel_id).toBe(channel.id);
  // Same place, same size, as requested of the new window.
  const shellLog = readFileSync(path.join(shell!.runtimeDir, "logs", "server.log"), "utf8");
  expect(shellLog).toContain(`window → Second (${second.url}) at ${before.width}x${before.height}+${before.x}+${before.y}`);
  // The local server was never stopped.
  expect(await serverIsUp(local.url)).toBe(true);
  expect((await list(workbench)).find((entry) => entry.id === "local")?.status).toBe("running");
  // The next launch opens where the window is now.
  const settings = JSON.parse(readFileSync(path.join(shell!.runtimeDir, "settings.json"), "utf8")) as { activeServer: string };
  expect(settings.activeServer).toBe(target.id);
});

test("a probe of the window's own server tests the token it names, not the window's bound one (MS-02)", async () => {
  const second = remotes[1]!;
  const info = await workbench.evaluate(() => (window as unknown as WindowWithBridge).collimateDesktop.server);
  expect(info).toMatchObject({ url: second.url });
  const probe = (token?: string) => workbench.evaluate(({ url, token }) =>
    (window as unknown as WindowWithBridge).collimateDesktop.servers.probe(token ? { url, token } : { url }),
  { url: second.url, token });
  const lastSession = () => readFileSync(path.join(second.root, "requests.jsonl"), "utf8").trim().split("\n")
    .map((line) => JSON.parse(line) as RequestRecord).filter((row) => row.path === "/auth/session").at(-1);

  // The window's session injects the bound token for this origin; the
  // shell's own requests must not pick it up (Change token, Test, re-add).
  const wrong = `wrong-${randomUUID()}`;
  expect(await probe(wrong)).toMatchObject({ ok: false });
  expect(lastSession()?.authorization).toBe(`Bearer ${wrong}`);
  expect(await probe()).toMatchObject({ ok: false });
  expect(lastSession()?.authorization).toBe("");
  expect(await probe(second.token)).toMatchObject({ ok: true });
  // The window itself still rides its bound token.
  await privateAccess(workbench, second);
});

test("quitting stops the local server and leaves both separately owned remote APIs alive __P0F32", async ({ browserName: _browserName }, info) => {
  const local = (await list(workbench)).find((entry) => entry.id === "local")!;
  await shell!.trace.beforeClose(info, workbench);
  await shell!.app.close();
  await expect.poll(() => serverIsUp(local.url), { timeout: 60_000 }).toBe(false);
  expect(existsSync(path.join(shell!.runtimeDir, "server.json"))).toBe(false);
  for (const remote of remotes) {
    expect(await serverIsUp(remote.url)).toBe(true);
    expect(remote.child.exitCode).toBeNull();
    expect((await fetch(`${remote.url}/api/state`)).status).toBe(401);
    expect((await fetch(`${remote.url}/api/state`, {
      headers: { Authorization: `Bearer ${remote.token}` },
    })).status).toBe(200);
  }
});
