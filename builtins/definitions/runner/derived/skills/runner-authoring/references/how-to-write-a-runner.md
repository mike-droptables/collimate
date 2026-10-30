# How to write a runner

A runner is a complete saved invocation of one generator. Choosing it runs it immediately. To change its prompt, settings, files, security or accounts, edit and save it first in the Runners group of Capabilities → Manage. Search tags are labels, never alternate configurations.

Runners live in Capabilities cells:

```
my-runner/invocation.runner
my-runner/files/_skills/task/SKILL.md
```

A runner package holds one `.runner` definition and its supplemental `files/`.
Exactly one definition file is allowed across all four kinds (`.generator`,
`.renderer`, `.runner`, `.handler`), which all establish discovery boundaries.
The declared `id` identifies it across the channel; neither the package folder
nor definition filename is its identity. Discovery takes the shallowest
definitions, skips top-level `_runs/`, and refuses a definition at the cell root. Nested
definitions are package content. An ambiguous folder establishes a boundary
and is refused.

```yaml
id: my-runner
name: My runner
description: A saved task over the target cell.
generator: agent
tags: [writing]
prompt: Rewrite the target files clearly.
settings:
  agent-config.set_yaml:
    surface: annotation
    session: oneshot
    backend: pydantic-ai
    system_prompt: ''
    mcp: 'off'
    approvals: auto
    selected_file_first: false
folders: [_skills]
accounts:
  model: local-model-option-id
permissions:
  docker: none
  internet: full
  commands: deny
packages: {}
```

`settings` contains every field of the generator's main configuration exactly once (the example abbreviates the Agent's; its shipped presets list them all). Files live under `files/`; the main configuration must never be copied there. Folder names must match the generator's declared `config_folders`. A runner is not allowed to change generator source code.

`accounts` maps declared generator roles to global Key Manager option IDs. Store no keys, tokens, passwords or credential documents in the runner. Imported runners with unresolved local references remain unready until the user binds valid options.

Security belongs to the saved recipe. Docker levels are `none`, `limited`, `full`. `none` runs the generator on the host without permission to launch Docker containers. `limited` runs the generator in one container without permission to launch more containers. `full` runs the generator on the host and allows it to launch containers, including agent harnesses. Restricted network access needs `docker: limited`. A generator that declares `requires_docker` needs `docker: full` on every runner; one that declares `requires_docker_if` needs it on a runner whose settings meet that condition (the Agent's `surface: terminal` on a harness whose CLI runs in its container). `collimate-lint` reports `runner-docker-required` for a runner without it, and the app refuses to save or start one. With `internet: proxy`, `internet_allow` lists the hosts the run may reach: each entry is a bare host name that also covers its subdomains (`example.com` covers `sub.example.com`), and an entry with a wildcard, scheme, path or port never matches. Commands levels are `deny`, `ask`, `allow`; see [Commands](#commands). An account binding grants only its explicit role.

Where a harness runs follows the grant. Container harnesses use their
containers under `docker: full`; under `none`, codex, goose and opencode need a
usable installed host CLI, and the others need `full`. The host-locus harnesses
— pydantic-ai (Pydantic AI's own ACP adapter, which Collimate installs into an
environment of its own) and Claude Code (the installed CLI when usable, else
its adapter's bundled CLI, with the choice logged) — run on this machine under
`none` or `full`, and their commands, where the grant allows them, run on this
machine too — except pydantic-ai's under `docker: full`, which run in a sandbox
container (the run workspace mounted as for every harness container).
`docker: full` is recommended for pydantic-ai with `commands: allow`. No
harness runs under `limited`.

The CLI surface runs where the harness's terminal locus puts it for the
selected account: in the harness's container under `full`, or, for Claude
Code and for Antigravity on the Google plan, as the vendor's own CLI on this
machine (`claude`, `agy`) under `none` or `full`. A CLI on this machine needs
`commands: ask` (its own prompt asks before each command) or `allow`
(unattended): nothing can hold a deny on an interactive terminal here, so
`deny` refuses it. It also needs that CLI installed on this machine, and it
runs outside any sandbox. It keeps its own sign-in in a per-account home under
the host cache (`<cache>/collimate/host-homes/<harness>/<account>/`, owner-only,
never in a workspace), so you sign in once, in the CLI's own terminal and
through the vendor's own flow, per Key manager entry, and later runs of every
cell on that entry reuse it. Collimate never starts, reads or copies that
sign-in. A Claude subscription entry's setup token, when it has one, is used
instead.

An Annotation runner of the Agent generator adopts the least grant its
harness needs (the Agent's manifest declares `adopts_harness_grant: true`; no
other generator's runner is ever raised): when the selected model's harness
needs `docker: full` and the runner grants less, the run starts with `docker`
raised to `full`, the run's `run.yaml` and runner
snapshot record the runner's grant beside the effective one (`placement`), and
its card and status row say the grant was adopted. Only `docker` is raised,
never `commands`, and never on a restricted network (`internet: proxy` or
`none` keeps the runner on `limited`, so the run is refused). A harness that
runs on this machine under `none` is never raised: under `limited` it is
refused, because `none` already places it. A Chat or CLI
runner's grant is its own: one that cannot place its harness stops before work
starts. A runner that already grants enough is kept as it is.

Web tools follow `internet`: enabled under `full`/`proxy`, disabled under
`none`. Remote models need network access; proxy allowlists must cover the
model endpoint host. Only loopback and explicit host-gateway endpoints are
offline exemptions. A subscription transport whose actual model host cannot be
verified refuses `proxy` and needs `full`. `mcp: http_stdio` needs `full` and a
containerized harness. A terminal run never falls back to another surface.
`approvals: ask` or `commands: ask` requires an answerable surface. Impossible
grants stop before work starts and name the required permission or local
installation fix.

Named configurations belong to the generator. Loading one copies its complete settings, prompt, files and security into an editable runner. Existing saved runners remain unchanged when a config changes. See [generator authoring](how-to-write-a-generator.md).

Run `collimate-lint generators --strict` before sharing a cell. An exported cell carries its runners' declared packages and supplemental file bodies; secrets remain local.

## The Agent generator

The Agent cell ships one generator, `agent`, whose `surface` setting picks
where you work with the run: `annotation` (Annotation, the default), `chat`
(Chat) or `terminal` (CLI). A run ends when you stop it, when a terminal's
program exits, or when a one-shot ends itself. Its presets are one per harness
(Agent on pydantic-ai, Claude Code, Antigravity, Codex, goose, opencode, Qwen
Code, Cline, pi, Hermes, Mistral Vibe and GitHub Copilot CLI, all interactive on
Annotation), the eight one-shots (Annotation with `session: oneshot`) and Guide
and Superpowers (Chat).

- `annotation` — you post messages to the run from the cell's status pane,
  queued or interrupting what it is doing, pointing at files and at elements
  inside them. `session: interactive` (the default) keeps the run listening
  until you stop it: whenever it is idle and messages wait, one turn reads them
  and answers in their threads with the `annotate` tool (`list_annotations`
  reads the whole context); a turn that posts nothing has its final answer
  posted under the batch's last thread. An interrupt cancels the running turn,
  whose messages come back first in the next. `session: oneshot` takes the
  prompt as the run's first message, answers it in one turn, replies under it
  and ends once its corrections settle. The run writes no UI file; its approval
  cards appear in the run's view in the status pane. Host-refused writes get at
  most two consecutive correction turns per path.
- `chat` — a conversation backed by the run's chat pointer and transcript. The
  runner's prompt is an optional opening message. Past messages are read-only
  so the transcript stays in sync with the harness. Host-refused writes get
  correction turns as on Annotation.
- `terminal` (CLI) — the harness's own TTY behind the pointer
  `terminal/<backend>.terminal` in the run folder: in its container, or on
  this machine for Claude Code and for Antigravity on the Google plan (see
  above). The generator declares `requires_docker_if` for the harnesses whose
  CLI always runs in its container, so such a terminal runner needs `docker:
  full`. It needs a harness with a TTY (claude-code, codex, opencode, goose,
  qwen-code, cline, pi, antigravity, hermes, vibe or copilot; pydantic-ai has
  none), and takes no `mcp` or `approvals`. The runner's prompt shows only for
  claude-code, whose TTY takes it as its first message. Claude-code, codex,
  goose and opencode run the TTY on the run's own session, so the live
  transcript follows exactly that conversation; antigravity's TTY starts one of
  its own, which the transcript follows once it appears. A TTY whose program
  exits ends the run; one that cannot start ends it failed.

`selected_file_first` ("Start from the selected file", off by default) makes
the file selected when the run starts part of its first turn: a one-shot's
opening turn, the first batch of messages an interactive annotation run
reads, the first chat turn, or claude-code's first TTY message. The runner
editor shows it on the terminal surface only for claude-code, the one harness
whose TTY takes a first message. With it on, the runner takes the selected
file (`takes_selected_file_if`) and the runner picker names the file it will
use.

A run's folder includes its host-owned `<label>.run` card plus its generator's
UI: `chat/conversation.chat` and `chat/main.json`, or
`terminal/<backend>.terminal`; an annotation run has none. Terminal
transcripts and harness exports are logs under `_agent/terminal/`. At run end
the run-folder files move to logs and the host sweeps the folder; work output
stays.

To carry work to a later run, ask the run for a summary or a handoff: it
writes `<name>.llm_summary` at the cell root (markdown, shown as markdown)
through the Agent's `llm-summary` skill, and a later run on any surface picks
up from it when you ask. Nothing writes one automatically, and a run never
continues a previous run's harness conversation. The agent stages its own
skills — `llm-summary` on every surface, plus `annotations` on Annotation —
beside the runner's; accepted handler skills are read through the handler
tools, and type-specific validation belongs to the handler.

The Agent stages disposable harness homes under workspace `.collrun/homes/`; private state stays out of cell exports. pydantic-ai's home (its session store and instruction file) is under the host cache instead, where its command sandbox, which mounts the workspace, cannot reach it, and its provider key reaches the server on an inherited pipe, never a file. The host maintains a temporary credential document shared by concurrent runs of the same selected account generation and authorizes only its registered file/directory bindings. Pinned Codex, OpenCode and Pi writers use a shared file bind; Cline binds the directory because its writer replaces files atomically. Release collects refreshes with generation checks and writeback locking and revokes the run's binding authority. Read-only policy text, including Antigravity's command guard, is materialized and bound by the host outside the workspace; no generator chooses that host source path.

## Commands

The `commands` level says whether the agent may run shell commands it chooses. It applies to a generator that declares `enforces: [commands]` in its manifest: the Agent cell's `agent`. The host has no enforcement point for these commands, so that generator enforces the level itself. The runner editor offers the level only for such a generator, and `collimate-lint` warns that a level set on any other generator's runner is not enforced by that generator. `launch`, `web` and `git` are outside the axis: they run the commands their own settings name, and an IDE agent inside a launch runner (the VS Code runner's Claude Code and Cline) is driven by the human at the keyboard and asks under its own prompts.

On the Agent, the level sets each harness's own setting for its shell tool: `deny` removes the tool, `ask` has the harness ask before each command (a card in the chat, the harness's own prompt in the terminal), and `allow` runs commands unattended, which with a host CLI under `docker: none` means on this machine. Under `allow` the harness does not ask, so `approvals: ask` shows no card for its commands; it cards only what a harness still asks about. An Agent runner that omits `commands` gets `deny`. The host-locus harnesses (pydantic-ai, and claude-code's chat) run on this machine whenever their grant permits the run, so `allow` there is unattended here too — except pydantic-ai under `docker: full`, whose commands run in its sandbox container; `collimate-lint` warns about `allow` on this machine. A level a harness's own mechanism cannot hold resolves down to the strictest one below it, with a notice (Hermes holds `deny` and `allow` only).

A terminal in its container is available at every commands level. Under `deny` the agent has no shell tool there; a `!`-prefixed line the user types is the user's own command and runs in the container. A terminal on this machine is refused under `deny`; under `ask` and `allow` its CLI's own settings carry the level, as in the container, and a `!`-prefixed line runs on this machine.

A command card shows the whole command, or the whole raw input when no command is recognised in it. A command longer than 16,000 characters is refused rather than carded, in the chat's cards under `ask` and under `approvals: ask`. A request or terminal that arrives after its turn stopped is refused.

The cell's own files (harness config, hooks, plugins, skills, rules, agent definitions) are trusted and load as the harness loads them. That includes files the agent writes into the workspace with its file tools, which `commands` does not restrict at any level: a hook, customization folder, command or skill template, or `.git/config` the agent writes runs as the harness runs it. Collimate writes its setting to the harness's highest tier (system, managed or command-line settings, root-owned where they sit in the harness home), so the cell's config adds to it rather than undoing it wherever the harness's precedence allows; the table names the exceptions.

| Harness | `deny` · `ask` · `allow` | Not covered by a card |
| --- | --- | --- |
| pydantic-ai | Coder's own tools: `deny` removes the `shell` tool, `ask` makes each shell call a permission request the chat cards before it runs, `allow` runs it unattended. Under `docker: full` the host places the run's sandbox container (the workspace mounted at `/workspace`) and Coder's workspace — its shell and file tools — is that container, recommended for `allow`; under `docker: none` the shell runs on this machine in the run's workspace. Delegation (`delegate_task`) is offered only under `allow`, as a delegate runs outside the session's approvals. | Coder's file tools write the workspace directly, and its repository context loads the cell's instruction files (`AGENTS.md` and its kin), including ones the agent writes. |
| claude-code | A `--settings` deny or ask rule on Bash, Monitor and PowerShell, which a cell's allow rule cannot lift; `allow` pre-approves Bash (chat) or skips prompts (terminal). In the chat, Accept Edits is allowed under `deny` and refused under `ask`, and `allowManagedPermissionRulesOnly` in file-based managed settings (`managed-settings.json`, `managed-settings.d/`) lowers `ask` to `deny` with a status notice; MDM, the Windows registry and server-managed organization settings are not detected. MCP tools in the chat need claude-agent-acp 0.81 or later, which marks their requests (`_meta.claudeCode.mcpServer`); with an older adapter pinned, their requests are refused. The chat starts the MCP servers in the cell's own `.mcp.json` beside Collimate's, passed from that file: the CLI's own project scope would also start those in every `.mcp.json` above the workspace, which on this machine reaches the user's home. | The cell's hooks, skills and `.mcp.json` servers (in the chat they start on this machine), including ones the agent writes. |
| codex | `deny`: no shell tool. `ask`: on-request approvals the user answers, in the workspace-write sandbox (the chat's `read-only` preset, and the same in the terminal). `allow`: full access. Code mode is off under `deny` and `ask`. codex asks before an MCP tool call (Collimate's tools and the cell's carry no read-only hint, so it asks under `deny` and `ask`), and those asks follow `approvals`, not `commands`. | Under `ask`, a command that stays inside codex's workspace-write sandbox runs without asking, where that sandbox can start: it does with a host CLI under `docker: none`, but in the container it usually cannot (Docker's default seccomp and AppArmor profiles refuse bubblewrap's user namespace), so there every command fails inside the sandbox until the agent asks to run it outside, and that request is carded. With a host CLI under `docker: none`, input the agent types into a command the user approved also runs without a card, because nothing on this machine turns codex's unified exec off (in the container a requirements file does). In the terminal, an "allow for this session" or command-prefix answer the user gives lets later matching commands run without asking. The cell's exec-policy rules and hooks in `.codex/`, including ones the agent writes: hooks run without codex's hook review (`--dangerously-bypass-hook-trust` in the terminal, its thread setting in the chat). |
| antigravity | The chat runs Google's `antigravity-acp` server: the client answers each of its `session/request_permission` requests by the level (its exec tools are `run_command` and `shell`), and only its `default` mode is taken (`auto_edit` and `yolo` approve without asking). Below `allow` the server's tools that run or start work without a request of their own (`run_workflow`, `manage_task`, `schedule` and the subagent tools) are withheld, and a request that names one is treated as a command; under `deny` `run_command` is withheld too. The terminal runs agy itself under permission rules plus Collimate's PreToolUse hook on every exec tool (input typed into a running command through `manage_task` included): on a Gemini key in its container, from a host-materialized read-only global hook binding whose source the generator cannot replace; on the Google plan as this machine's agy, with the same rules and hook in its per-account home's global hooks file, which on this machine is not protected from agy; `allow` runs it with the skip flag. In the terminal under `ask`, input typed into a running task is refused rather than prompted, as agy cannot prompt for it; the user types into the task themselves. | The cell's hooks and plugins in its customization folders (`.agents/`, `_agents/`, `.agent/`, `_agent/`), including ones the agent writes. An "Allow Always" a human gives on a non-command tool under `approvals: ask` persists in the server's session metadata. Collimate records the workspace (that folder only) as trusted before each terminal start, so agy never shows its folder-trust prompt. On a Gemini key it also marks agy's first run complete, so agy opens at its prompt in its default colours and never shows its "Terms of Service & Data Use" screen: Collimate accepts those terms for the user. On the Google plan those first-run screens are agy's own, answered once at sign-in in the per-account home. That records no data-use opt-out, as agy keeps no data-use answer under an API key, and agy's telemetry client still posts to `play.googleapis.com` on every turn. Collimate blocks no telemetry: the decline is the Google account's own setting, made once per account by unticking the box on agy's screen at sign-in (on the host), and every run under that login inherits it. |
| goose | `deny`: the developer extension without `shell`. `ask`: approve mode. `allow`: auto. A session opened under a looser level is never resumed, as goose restores a session's stored extensions. | Under `deny` and `ask`, summon, the extension manager and the scheduler are not loaded, because their delegates and recipes run unattended. In the terminal, an "Always Allow" answer or `/mode auto` the user gives lets later commands in that terminal run without asking. |
| opencode | `permission.bash` in the root-owned global config; under `ask` the chat also turns subagents (`task`) off, as it cannot show their asks. Under `deny` and `ask` snapshots are off (they run git on a git directory in the harness home) and its git reads no global or system git config; on this machine that also sets aside the user's own `~/.gitconfig` for commands approved under `ask`. | The cell's `opencode.json` and `.opencode/` merge after it, so a cell can set `bash` above the grant. Commands made only of `cd` or variable assignments never ask, unless a `cd` leaves the workspace. The cell's command and skill templates, including ones the agent writes, run their `` !`cmd` `` lines without asking. In the container `~/.opencode` is root-owned; with `docker: none` the user's own `~/.opencode` merges after Collimate's config. |
| qwen-code | A system-settings deny or ask rule on `run_shell_command`, and under `ask` the subagent tool denied (a subagent with its own approval mode would not ask); `allow` runs yolo. Safe and bare mode, which drop every permission rule, are set off. Bare mode has no off flag, and a cell `.env` that turns it on (`QWEN_CODE_SIMPLE`) reaches a later session the same qwen process opens, so under `deny` the shell is also excluded on the command line and the chat opens one session per qwen process. | The cell's hooks, including ones the agent writes, and the auto-memory agents (they run in yolo). `enter_worktree` runs git under the repository's own `.git/config`, which can name commands git runs (`core.fsmonitor`). |
| cline | `deny`: `run_commands` disabled. `ask`: cline's own approval flow, auto-approve off, and no sub-agents or teammates (theirs would not ask). Under both, Collimate writes the settings and an MCP settings file holding only its own servers afresh each leg, outside the harness home; they stay root-owned in the chat, and in the terminal they are handed to the user so cline can save its own settings. | Hooks and plugins, under cline's own trust model. In the terminal, Shift+Tab or `/yolo`, the user's own choice, turns auto-approve on: from then on the agent's edits, those settings files included, run without asking. |
| pi | A root-owned wrapper: `deny` excludes bash and powershell, `ask` loads a gate that confirms each call. The harness home's `models.json`, and an API-key runner's `auth.json`, are rewritten each time (their `!` values run as shell): `models.json` holds only the provider registry's endpoint as the provider's `baseUrl`. | Project extensions and settings, including ones the agent writes: the wrapper passes `--approve`, so they load in the chat and the terminal without a trust prompt. |
| hermes | A root-owned managed config (`/etc/hermes`): `deny` removes the terminal and code-execution toolsets, `allow` turns approvals off. `ask` resolves to `deny` with a notice. The browser toolset is always off, and below `allow` so are the scheduled-job and delegation toolsets. The managed config also pins off every Hermes feature that turns a file in its home into a command (inline shell in skills, shell hooks and their allowlist, plugins, language servers, SQLite extensions, MCP servers), and each leg clears the home entries those features load from. | Nothing the agent writes into the Hermes home runs a command below `allow`. |
| vibe | Policy in the `VIBE_*` environment the agent cannot rewrite: `deny` disables `bash`, `ask` asks with an empty allowlist, `allow` turns auto-approve on; below `allow` `VIBE_BYPASS_TOOL_PERMISSIONS` is off and no mode that approves without asking is taken. Every file in the Vibe home that can name a command (`hooks.toml`, `config.toml`, `.env`, `trusted_folders.toml`, and the `tools`, `agents`, `plugins` and `prompts` folders) is cleared before each leg and placed read-only in the container, and no folder is trusted. | MCP tools run through `run_typescript`, whose code calls the enabled tools. With `allow`, permitted commands can read the provider key from the harness's environment. |
| copilot | Server-wide tool flags: `deny` excludes and denies the shell tool, `ask` leaves each shell call to Copilot's permission request (input typed into a running command, `write_bash`, is a command too), `allow` passes `--allow-tool shell`; below `allow` the `allow_all` option and autopilot mode are refused. Copilot's built-in MCP servers are off. | Copilot loads a workspace's hooks under `--acp` whether or not the folder is trusted, so Collimate trusts no folder and the image's root-owned managed settings lock hooks, custom agents and skills to plugins: files the agent writes there never run a command, at any commands level. Permitted commands run with the token or provider key stripped from their environment (`--secret-env-vars`). |

Verified from source rather than documentation: goose's `EXTENSIONS` allowlist, and its `session/load` and `--resume` restoring a session's stored extensions; cline's `CLINE_GLOBAL_SETTINGS_PATH`, `CLINE_MCP_SETTINGS_PATH`, `disabledTools` and `toolAutoApprove`; pi-acp's `PI_ACP_PI_COMMAND`; codex-acp sending `CODEX_CONFIG` as each thread's config, where codex reads `bypass_hook_trust`; the claude CLI reading a project `.mcp.json` in every folder from the workspace up to the root; opencode applying the global rules after each agent's own; qwen-code merging permission rules from every scope, a `.env` never overriding a variable already set at startup but applied again as each chat session opens, and `--exclude-tools` adding its deny rule after safe and bare mode drop the settings' rules; claude-agent-acp passing `settingSources` through to the CLI's `--setting-sources`. The versions read: goose 1.50.0, cline 3.0.61, pi-acp 0.0.33 with pi 0.85.1, codex-acp 1.11.0 with codex 0.153.4, opencode 1.18.30, qwen-code 0.23.3, claude-agent-acp 0.81.1 with claude 2.1.280, agy 1.2.1. The live matrix at those versions is the regression check; a newer release is verified only when that matrix passes on it.

The scripted-provider conformance suite (`tests_live/test_live_harness_conformance.py`: each harness billing its provider at the registry endpoint, which the suite points at a local scripted model) passed every level on every harness on 2026-10-25, in the chat leg, at claude-agent-acp 0.95.0 with its bundled claude, pydantic-ai-harness 0.61.0, antigravity-acp 1.4.3, codex 0.164.0 with codex-acp 2.2.2, goose 1.56.0, opencode 1.19.4, qwen-code 0.26.1, cline 3.0.73, pi 1.4.0 with pi-acp 0.0.34, Hermes Agent 0.22.5, Mistral Vibe 3.0.2 and GitHub Copilot CLI 1.0.103. The source-verified claims above stand at the versions read; these releases were run, not re-verified from source.


Launch mirrors human edits as run writes, so accepted handlers validate them
before they enter the cell. A refused file stays in the workspace and is logged
as skipped; it is not restored from the cell or retried until its bytes change.
Launch catches document `PermissionError` refusals at its flush boundaries and
continues serving the environment. Transport and host-commit failures remain
errors. Other accepted files in the same flush still commit.
