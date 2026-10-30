// Collimate's gate for pi under the runner grant's `commands` level (deny or
// ask — collimate-pi loads it for both; allow runs without it). Root-owned in
// the image beside the wrapper.
//
// - ask: every bash/powershell call waits for a yes through ctx.ui.confirm,
//   titled "collimate:command" (pi-acp forwards it to the ACP client as a
//   permission request, which the client cards; the TUI shows its own
//   dialog). No UI to ask through blocks the call.
// - deny: the wrapper already removed the shell tools; a call that still
//   arrives is blocked.
// - both: write/edit outside /workspace is blocked — pi's home holds the
//   settings Collimate writes and files whose `!`-prefixed values it runs as
//   shell (models.json).
//
// A `!cmd` line typed in the TUI is the human's own command and runs.
import { realpathSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const SHELL_TOOLS = new Set(["bash", "powershell"]);
const WRITE_TOOLS = new Set(["write", "edit"]);
const WORKSPACE = "/workspace";
const UNICODE_SPACES = /[  -   　]/g;

const level = process.env.COLLIMATE_COMMANDS === "ask" ? "ask" : "deny";

// The path a write/edit tool will touch, resolved the way pi's own
// resolveToCwd does (unicode spaces, a leading "@", "~", file:// URLs),
// then through the symlinks of its deepest existing ancestor.
function resolveToolPath(raw: string, cwd: string): string {
  let p = raw.replace(UNICODE_SPACES, " ");
  if (p.startsWith("@")) p = p.slice(1);
  if (p === "~") p = homedir();
  else if (p.startsWith("~/")) p = join(homedir(), p.slice(2));
  else if (/^file:\/\//.test(p)) p = fileURLToPath(p);
  let abs = isAbsolute(p) ? resolve(p) : resolve(cwd, p);
  const rest: string[] = [];
  for (;;) {
    try {
      const real = realpathSync(abs);
      return rest.length ? join(real, ...rest.reverse()) : real;
    } catch {
      const parent = dirname(abs);
      if (parent === abs) return resolve(p);
      rest.push(abs.slice(parent.length).replace(/^\//, ""));
      abs = parent;
    }
  }
}

function insideWorkspace(path: string): boolean {
  return path === WORKSPACE || path.startsWith(WORKSPACE + "/");
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event, ctx) => {
    const input = (event.input ?? {}) as Record<string, unknown>;
    if (WRITE_TOOLS.has(event.toolName)) {
      const target = resolveToolPath(String(input.path ?? ""), ctx.cwd || WORKSPACE);
      if (!insideWorkspace(target)) {
        return { block: true, reason: `collimate: ${target} is outside ${WORKSPACE}` };
      }
      return undefined;
    }
    if (!SHELL_TOOLS.has(event.toolName)) return undefined;
    if (level !== "ask") {
      return { block: true, reason: "collimate: shell commands are denied for this run" };
    }
    if (!ctx.hasUI) {
      return { block: true, reason: "collimate: no one to approve this command" };
    }
    const ok = await ctx.ui.confirm("collimate:command", String(input.command ?? ""));
    return ok ? undefined : { block: true, reason: "collimate: the user did not approve this command" };
  });
}
