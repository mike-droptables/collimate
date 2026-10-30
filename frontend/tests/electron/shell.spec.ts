import { spawn, type ChildProcess } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { readFileSync, rmSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import {
  launchShell,
  missingPrerequisite,
  serverIsUp,
  workbenchWindow,
  type LaunchedShell,
} from "./desktop";

/**
 * The desktop shell, end to end, with nothing faked.
 *
 * `desktop/tests/*` drive the ServerController and the guest manager
 * against fakes, which is how the timeout ladders and the authorisation
 * chokepoint get tested at all. This suite answers the question those
 * can't: that the real shell, on the real Electron binary, actually
 * starts the real server, points a window at it, exposes the bridge,
 * creates a real WebContentsView guest on request, scripts and captures
 * it, refuses an id it never minted, and takes the server with it when it
 * quits.
 *
 * Requires a display. On a headless runner:
 *   xvfb-run -a npx playwright test -c playwright.electron.config.ts
 *
 * Note on the page.evaluate bodies below: they run in the window's own JS
 * world, so they can't call anything from this module's scope. The bridge
 * lookup is written out in each one on purpose.
 */

const blocked = missingPrerequisite();
test.skip(blocked !== null, `desktop e2e prerequisites are missing: ${blocked ?? ""}`);

test.describe.configure({ mode: "serial" });

const GUEST_PARTITION = "persist:collimate-e2e";
const GUEST_ID = "e2e-guest-1";

/** The bits of `window.collimateDesktop` this suite touches. */
interface DesktopBridge {
  shellVersion: string;
  bridge: number;
  capabilities: Record<string, boolean>;
  server: { id: string; kind: string; name: string; url: string; port: number };
  guest: {
    createGuest(options: {
      guestId: string;
      partition: string;
      url: string;
    }): Promise<{ guestId: string; url: string }>;
    destroyGuest(guestId: string): Promise<void>;
    evaluate(guestId: string, js: string): Promise<unknown>;
    capturePng(guestId: string): Promise<ArrayBuffer>;
  };
  onGuestEvent(
    handler: (event: { guestId: string; kind: string; data: unknown }) => void,
  ): () => void;
}

type WindowWithBridge = Window & { collimateDesktop?: DesktopBridge };

let shell: LaunchedShell | null = null;
let workbench: Page;
let serverUrl = "";
let sentinel: ChildProcess | undefined;

test.beforeAll(async ({ browserName: _browserName }, info) => {
  sentinel = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
    stdio: "ignore", detached: true, env: {},
  });
  if (!sentinel.pid) throw new Error("preboot sentinel did not start");
  shell = await launchShell({ prebootSentinel: {
    pid: sentinel.pid, startedAt: Date.now() - (os.uptime() + 120) * 1000,
  } });
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
  if (sentinel && sentinel.exitCode === null && sentinel.signalCode === null) {
    const exited = new Promise<void>((resolve) => sentinel!.once("exit", () => resolve()));
    sentinel.kill("SIGTERM");
    await exited;
  }
  // Idempotent: the last test closes the app itself, and a failure
  // anywhere earlier must still not leave a server holding a port.
  if (shell) {
    await shell.app.close().catch(() => undefined);
    rmSync(shell.userDataRoot, { recursive: true, force: true });
    shell = null;
  }
});

test("opens the workbench on a loopback server it started itself __F1060_F1091_F1918_P0F32", async () => {
  expect(workbench.url()).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/$/);
  // Not the default port: the shell allocates an ephemeral one so a second
  // Collimate, or anything else on 5001, can't collide with it.
  expect(new URL(workbench.url()).port).not.toBe("5001");
  expect(await serverIsUp(serverUrl)).toBe(true);

  const info = await workbench.evaluate(() => {
    const desktop = (window as WindowWithBridge).collimateDesktop;
    if (!desktop) throw new Error("window.collimateDesktop is not there");
    return { ...desktop.server, bridge: desktop.bridge, shellVersion: desktop.shellVersion };
  });
  expect(info).toMatchObject({ id: "local", kind: "local", name: "Local" });
  expect(info.url).toBe(serverUrl);
  expect(info.port).toBe(Number(new URL(serverUrl).port));
  expect(info.bridge).toBe(6);
  // Baked in at build time; "0.0.0-dev" means the define didn't run.
  expect(info.shellVersion).not.toBe("0.0.0-dev");
  expect(await shell!.app.evaluate(({ app }) => app.getPath("userData"))).toBe(shell!.runtimeDir);
  expect(sentinel?.exitCode).toBeNull();
  expect(sentinel?.signalCode).toBeNull();
  expect(() => process.kill(sentinel!.pid!, 0)).not.toThrow();
  expect(readFileSync(path.join(shell!.runtimeDir, "logs", "server.log"), "utf8"))
    .toContain(`server.json (pid ${sentinel!.pid}) predates this boot; dropping it without signalling`);
  const record = JSON.parse(readFileSync(path.join(shell!.runtimeDir, "server.json"), "utf8"));
  expect(record.pid).not.toBe(sentinel!.pid);
  expect(record.port).toBe(info.port);
  if (process.env.COLLIMATE_E2E_TRACE_PROOF === "1") {
    expect(false, "ELECTRON_TRACE_PROOF deliberate failure").toBe(true);
  }
});

test("exposes native guests as a capability", async () => {
  const capabilities = await workbench.evaluate(() => {
    const desktop = (window as WindowWithBridge).collimateDesktop;
    if (!desktop) throw new Error("window.collimateDesktop is not there");
    return desktop.capabilities;
  });

  expect(capabilities.nativeGuests).toBe(true);
  expect(capabilities).toMatchObject({
    nativeGuests: true,
    guestCapture: true,
    persistentPartitions: true,
    directLoopback: true,
    externalOpen: true,
  });
});

test("creates a guest through the bridge and scripts it", async () => {
  const created = await workbench.evaluate(
    async ({ partition, guestId }) => {
      const desktop = (window as WindowWithBridge).collimateDesktop;
      if (!desktop) throw new Error("window.collimateDesktop is not there");
      // The readiness signal is the main process's `ready` push — with no
      // DOM element there is no dom-ready to wait on. Subscribe before
      // creating so the event can't be missed.
      const ready = new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("the guest never finished loading")), 60_000);
        const unsubscribe = desktop.onGuestEvent((event) => {
          if (event.guestId !== guestId || event.kind !== "ready") return;
          clearTimeout(timer);
          unsubscribe();
          resolve();
        });
      });
      const state = await desktop.guest.createGuest({
        guestId,
        partition,
        url: `${location.origin}/`,
      });
      await ready;
      return state;
    },
    { partition: GUEST_PARTITION, guestId: GUEST_ID },
  );

  expect(created.guestId).toBe(GUEST_ID);

  // Over IPC, into a separately-sandboxed renderer the page does not own —
  // and back.
  const two = await workbench.evaluate(async (id) => {
    const desktop = (window as WindowWithBridge).collimateDesktop;
    if (!desktop) throw new Error("window.collimateDesktop is not there");
    return desktop.guest.evaluate(id, "1 + 1");
  }, GUEST_ID);
  expect(two).toBe(2);

  // The guest is hostile content by assumption: no bridge, no node.
  const exposed = await workbench.evaluate(async (id) => {
    const desktop = (window as WindowWithBridge).collimateDesktop;
    if (!desktop) throw new Error("window.collimateDesktop is not there");
    return desktop.guest.evaluate(
      id,
      "JSON.stringify([typeof window.collimateDesktop, typeof require, typeof process])",
    );
  }, GUEST_ID);
  expect(exposed).toBe('["undefined","undefined","undefined"]');
});

test("captures a guest that has never been shown", async () => {
  // The view was created invisible and no layout ever showed it — this is
  // exactly the daemon's keepAlive-screenshot path, and it has to work
  // without the view ever reaching the screen.
  const png = await workbench.evaluate(async (id) => {
    const desktop = (window as WindowWithBridge).collimateDesktop;
    if (!desktop) throw new Error("window.collimateDesktop is not there");
    const bytes = await desktop.guest.capturePng(id);
    return Array.from(new Uint8Array(bytes.slice(0, 8)));
  }, GUEST_ID);

  // The PNG signature: a real capture, not an empty buffer.
  expect(png).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
});

test("refuses a guest id it never minted", async () => {
  const refusal = await workbench.evaluate(async () => {
    const desktop = (window as WindowWithBridge).collimateDesktop;
    if (!desktop) throw new Error("window.collimateDesktop is not there");
    try {
      await desktop.guest.evaluate("no-such-guest", "1");
      return "resolved";
    } catch (error) {
      return error instanceof Error ? error.message : String(error);
    }
  });

  // The authorisation path: an id the page made up gets a typed refusal,
  // not a scripted webContents.
  expect(refusal).toContain("collimate-guest/unknown-guest");
});

test("refuses a partition outside the managed prefix", async () => {
  const refusal = await workbench.evaluate(async () => {
    const desktop = (window as WindowWithBridge).collimateDesktop;
    if (!desktop) throw new Error("window.collimateDesktop is not there");
    try {
      await desktop.guest.createGuest({
        guestId: "e2e-evil",
        partition: "persist:evil",
        url: `${location.origin}/`,
      });
      return "resolved";
    } catch (error) {
      return error instanceof Error ? error.message : String(error);
    }
  });

  expect(refusal).toContain("collimate-guest/bad-partition");
});

test("takes the server with it when the app quits", async ({ browserName: _browserName }, info) => {
  const app = shell?.app;
  expect(app).toBeDefined();
  if (!app) return;
  expect(await serverIsUp(serverUrl)).toBe(true);

  await shell!.trace.beforeClose(info, workbench);
  await app.close();

  // The shell asks the server to shut itself down and only escalates to
  // signals if that stalls; either way, nothing is left holding the port.
  const deadline = Date.now() + 60_000;
  let up = true;
  while (Date.now() < deadline) {
    up = await serverIsUp(serverUrl);
    if (!up) break;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  expect(up, `${serverUrl} still answered /health after the app quit`).toBe(false);
});
