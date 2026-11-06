// Collimate Bootstrap — a builtin extension baked into collimate-vscode-env.
//
// On startup, if the cell was launched with "Start Claude Code" checked, it
// closes any leftover editor tabs and opens Claude Code in a fresh
// conversation. code-server does NOT run commands from the workbench URL
// (the old `?command=` path was a silent no-op), so this is the reliable
// way to drive the editor into a known state on first paint.
//
// The flag is delivered two ways: the COLLIMATE_START_CLAUDE env var (inherited
// by the extension host; '1'/'0' wins outright) and, for any other value or
// none, a marker file. The entrypoint writes the marker when COLLIMATE_START_CLAUDE
// is unset or '1', so a normal start opens Claude Code.

const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');

// The extension's New Conversation command. claudeCode.preferredLocation is
// set to "panel" in the baked settings, which the extension labels
// "Panel (New Tab)" — so this opens Claude Code as a fresh editor tab.
const NEW_CONVERSATION = 'claude-vscode.newConversation';

function shouldOpenClaude() {
  const env = process.env.COLLIMATE_START_CLAUDE;
  if (env === '0') return false;
  if (env === '1') return true;
  // No env signal — fall back to the entrypoint's marker file.
  try {
    return fs.existsSync(path.join(os.homedir(), '.collimate', 'open-claude'));
  } catch (_e) {
    return false;
  }
}

// The Claude Code extension and this one both activate on startup, so its
// commands may not be registered yet. Poll until the command appears.
async function waitForCommand(id, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const commands = await vscode.commands.getCommands(true);
      if (commands.includes(id)) return true;
    } catch (_e) {
      // getCommands shouldn't throw, but never let it abort the bootstrap.
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return false;
}

async function activate() {
  if (!shouldOpenClaude()) return;

  // A fresh container with workbench.startupEditor:"none" and an empty
  // workspaceStorage shouldn't have any tabs, but close defensively so the
  // user lands on Claude Code alone regardless of any restored state.
  try {
    await vscode.commands.executeCommand('workbench.action.closeAllEditors');
  } catch (_e) {
    /* nothing open — fine */
  }

  if (await waitForCommand(NEW_CONVERSATION, 20000)) {
    try {
      await vscode.commands.executeCommand(NEW_CONVERSATION);
    } catch (e) {
      console.error('[collimate-bootstrap] failed to open Claude Code:', e);
    }
  } else {
    console.error('[collimate-bootstrap] Claude Code never registered its commands');
  }
}

function deactivate() {}

module.exports = { activate, deactivate };
