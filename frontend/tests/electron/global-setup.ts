import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { desktopDir, mainEntry, missingPrerequisite } from "./desktop";

/**
 * Build the shell before the suite runs.
 *
 * The spec drives `desktop/dist/main.cjs`, and a stale bundle is a test
 * that passes for the wrong reason. esbuild rebuilds it in well under a
 * second, so there is no reason to make the caller remember.
 *
 * What this can't do for you is `npm ci` followed by
 * `node node_modules/electron/install.js` in desktop/ for the pinned binary. Missing prerequisites are fatal under CI,
 * where the job installs them and a silent skip would be a false green,
 * and a warning anywhere else: a developer running the whole frontend
 * suite shouldn't be blocked by a desktop toolchain they never set up.
 * The spec skips itself, with the reason, in that case.
 */
export default function globalSetup(): void {
  const fatal = (message: string): void => {
    if (process.env.CI) throw new Error(message);
    console.warn(`[desktop-e2e] skipping: ${message}`);
  };

  if (!existsSync(path.join(desktopDir, "node_modules"))) {
    fatal("desktop/node_modules is missing — run `npm ci` in desktop/ before this suite.");
    return;
  }

  const build = spawnSync("node", ["build.mjs"], {
    cwd: desktopDir,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (build.status !== 0) {
    fatal(`building the desktop shell failed (exit ${String(build.status)}):\n${build.stderr || build.stdout}`);
    return;
  }

  const missing = missingPrerequisite();
  if (missing) {
    fatal(`cannot run the desktop e2e suite: ${missing}`);
    return;
  }
  console.log(`desktop shell built: ${mainEntry}`);
}
