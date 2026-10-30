# How to write a generator

A generator is a Python program plus a main settings schema and named configurations. Users run saved runners that target it. Generators do not carry runnable config tags.

```
my-generator/
  my-generator.generator
  main.py
  settings.set_yaml
  configs/default/manifest.config_yaml
  configs/default/files/templates/example.md
  configs/another/manifest.config_yaml
  configs/another/files/templates/example.md
```

A package is the folder holding exactly one definition file across all four
kinds (`.generator`, `.renderer`, `.runner`, `.handler`); a generator package
holds a `.generator`. Its filename and
folder name need not match its declared `id`; entrypoints are relative to that
folder. Stack discovery takes the shallowest definitions, skips top-level `_runs/`, and
refuses definitions at the cell root. Nested definitions belong to package
content. An ambiguous folder establishes the same boundary and is refused.

Write the entrypoint, main settings schema and declared configuration files
before the definition, then call `validate` on the `.generator` file. Its
accepted handler checks the schema and package references. The user's Add to
Capabilities approves activation; a definition written in an ordinary work cell
is inert.

The generator's materialized app tree and source revision contain its package
plus the cell's top-level `_*` shared folders, excluding top-level `_runs/`
and runner packages. Other packages in the cell are not part of its app tree.

```yaml
id: my-generator
name: My Generator
summary: Produce a greeting.
entrypoint: main.py
main_config: settings.set_yaml
draft_files: [settings.set_yaml]
configs:
  - {id: default, name: Default, path: configs/default/manifest.config_yaml}
required_files: [templates/example.md]
config_folders:
  - {name: templates, description: Templates used by the generator.}
account_requirements: {}
```

The main `.set_yaml` file contains `fields` with defaults. `advanced: false` fields appear in the runner editor's Config tab; `advanced: true` fields appear in its Advanced tab. Existing types, conditions (`show_if`, `required_if`, option `enable_if`), and validation apply. Secrets are never ordinary settings fields. Values are parsed as YAML 1.1 (PyYAML `safe_load`), so quote `on`, `off`, `yes`, `no`, `true` and `false` wherever a string is meant — an option or a default (`- 'off'`, `default: 'off'`) or a saved runner's setting (`mcp: 'off'`) — or the generator receives a boolean from `gen.config()`.

```yaml
fields:
  - key: greeting
    type: text
    label: Greeting
    default: Hello
    advanced: false
```

The prompt box is declared by `prompt_intent`; `prompt_required: true` makes a non-empty prompt a precondition for running. Both have conditional forms over the main config's own values, written in the same condition language as a field's `show_if` / `required_if`: `prompt_show_if` hides the box entirely when it does not match, and `prompt_required_if` demands a prompt only when it does. Either key without a `prompt_intent` is an error, and every field they name must be a field of the main config.

```yaml
prompt_intent: The task for a one-shot run — the first (and only) message the agent works on
prompt_show_if: {session: oneshot}
prompt_required_if: {session: oneshot}
```

A generator that works on the file the user is looking at declares `takes_selected_file: true`, or `takes_selected_file_if: <condition>` over its main config's dropdown and checkbox values (same condition language, `not` allowed). When the user starts a runner of it, the app sends the selected file: the active file tab of the cell (never a file under `_runs/`), else the files pane's selected file, else none — a run without a selection still starts. The host refuses a selected file for a runner whose generator does not take one, and one that is not in the cell when the run starts. The run reads the cell-relative path as `gen.selected_file` (`None` when there is none) and reads the content itself:

```yaml
takes_selected_file_if: {selected_file_first: true}
```

```python
path = gen.selected_file
if path is not None and gen.has_file(path):
    text = gen.read_file(path)
```

The canonical default uses main schema defaults and its default supplemental files. An explicit default configuration is permitted. Every named configuration carries complete main settings and the entire supplemental file set it needs. Loading is replacement, not a sparse overlay. Files listed in `required_files` must exist in every configuration, including default.

```yaml
# configs/default/manifest.config_yaml
name: Default
description: The standard greeting.
prompt: ''
settings:
  settings.set_yaml: {greeting: Hello}
folders: [templates]
accounts: {}
permissions: {docker: none, internet: full}
packages: {}
```

Named configs contain editable recipe data; runner IDs and generator targets are structural and are not config data. Supplemental content lives beside each config in `files/`. The main config and generator code are not supplemental files.

```python
import gen

async def run():
    greeting = gen.config('settings.set_yaml')['greeting']
    gen.update_file('greeting.md', greeting + '\n')

if __name__ == '__main__':
    gen.main(run)
```

`gen.config` returns the saved main values, `gen.prompt` the saved runner prompt, and `gen.list_files()`/`gen.read_file(path)` the run's own cell context. `gen.config_folder(name)` reads declared supplemental folder content. Output writes are staged with `gen.update_file` and flushed by `await gen.commit()` or successful completion. A supplied `name` may name only a cell the host created for this run; the host ignores it for an existing cell. Launch proposes the saved runner’s name, falling back to `Launch`. Report ongoing work through status and logs.

A run that judges its work can say how it came out: `gen.finish(note="3 of 4 checks passed", outcome="fail")` records a one-line `note` (whitespace collapsed, capped at 300 characters) and an `outcome`, `"pass"` or `"fail"` (anything else raises `ValueError`). It does not end the run; the run still ends when `run()` returns or raises, and that end carries what the last call recorded. The outcome is a verdict on the work, separate from whether the run worked: a probe that ran and found its check failing completes with `outcome="fail"`, while a crash is a failed run. The cell's journal shows the outcome as the run's pass or fail mark and the note under it; a run that sets neither is shown by its status, with the failure reason or stop as its note.

## Your run folder and locked files

Several runs can work in one cell at once, so each run has its own folder: `_runs/<label>/`, where the label is `<runner id>-<first 8 of the run id>` (`gen.run_info().label`, `.folder`). Every file in it is one of the run's **locked files** — the files that are the run's user interface: its pointers (`.chat`, `.terminal`, `.website`, `.webview`) and its transcripts.

**The run folder is for UI surface files ONLY.** It is not a working directory: work output (edits, cloned repos, summaries, captures and attachments) belongs elsewhere in the cell. The host refuses writes into another run's `_runs/<label>/` folder, the run's own card, and files in its own folder whose types the manifest's `ui_files` does not declare. It also checks writes covered by an accepted handler against the post-flush state and rechecks a touched package's definition. Invalid typed writes are refused as `handler_invalid`; accepted siblings still commit. `await gen.commit()` then raises `gen.PartialRefusal`, a `PermissionError` whose `paths` and `errors` name the refused paths and verifier errors. Handle that partial refusal without assuming accepted siblings were rolled back.

User edits follow a separate advisory path: saves always succeed independently of handler validation. The server validates after user commits and coalesced draft saves, including a package definition when its siblings change. Current per-file verdicts appear on editor tabs; newer content clears older results while validation is pending. A user save does not grant a generator permission to repeat an invalid write.

UI files are created only through one explicit API, and only for the types you declare. List their filename suffixes in the manifest:

```yaml
ui_files: [".terminal", ".website"]
```

Write one with `gen.update_ui_file(name, content)` — `name` is relative to the run folder or a full cell-relative path inside this run's folder (subfolders allowed) and the call returns its cell-relative path — and remove it with `gen.delete_ui_file(name)` (`log=True` keeps its last content in the run's Logs); `gen.list_ui_files()` lists them. `gen.serve_chat` and `gen.serve_terminal` write their pointers through the same path, and a pointer's renderer reads its live session with `api.getSurface()`. Declare the types you write (`.chat` and `.json` for a chat, `.terminal` for a terminal):

```python
pointer = gen.update_ui_file('preview.website', json.dumps(website_pointer(port)))
await gen.reveal_file(pointer)
async with gen.serve_terminal(container_id, ['bash'], pointer=gen.run_path('app.terminal')) as session:
    exit_code = await gen.wait_terminal_exit(session['terminal_id'])
```

- An undeclared suffix raises `ValueError` naming the manifest's `ui_files`, and `gen.update_file` / `gen.delete_file` raise `PermissionError` for every `_runs/` path, your own folder included. The host holds the same rule: it refuses an own-folder file of an undeclared type, so a harness writing straight into the workspace cannot slip one in (`gen.live_files()` removes it again and logs it).
- The host writes the run card, `_runs/<label>/<label>.run`, before your code starts and clears the folder when the run stops. Leave the card alone; remove your own UI files with `gen.delete_ui_file` when their session ends (the host's sweep at Stop is the backstop).
- Another run's folder is read-only: a write or delete there raises `PermissionError` at once, and the host refuses it as well. Another run's files are yours to read.
- `gen.serve_chat` and `gen.serve_terminal` require own-run paths.
- A run never pins its cell. To put a file in front of the user, reveal it: `await gen.reveal_file(path)` opens it as a tab of the cell (it flushes a staged `path` first).
- Everything outside `_runs/` is the cell's shared work: several runs may write the same files there, so a user who wants runs kept apart runs the second one in a new cell.

### Surfaces that show before the run starts

Declare the files a run serves as its surfaces under `opens`, so the user sees one the moment the run is accepted instead of when its process has spawned. Each entry is `{path, if?}`. `path` is a template: `{run}` (only as the first segment) is the run folder, `_runs/<label>`, and `{<key>}` is the resolved value of a main-config dropdown or text field; a path that starts with `{run}/` must end in a declared `ui_files` suffix, and any other path is a cell-scoped file. `if` is a condition over the main config's dropdown and checkbox values; an entry whose condition does not hold is skipped. `collimate-lint` checks all of it.

```yaml
ui_files: [".chat", ".json", ".terminal"]
opens:
  - {path: "{run}/chat/conversation.chat", if: {surface: chat}}
  - {path: "{run}/terminal/{backend}.terminal", if: {surface: terminal}}
```

When it accepts the run, the host resolves `opens` (`collimate.surfaces.resolve_opens(manifest, fields, values, folder=...)`), writes a placeholder for each path in the same version as the run card, claims the paths as **starting** surfaces and reveals the first one. A pointer file (`.chat`, `.terminal`, `.webview`, `.website`) gets `{"starting": true, "label": <runner name>}` (`collimate.pointers.starting_pointer`); any other path gets none and is claimed as it is. Your generator must tolerate that state:

- Its pointer already exists when `run()` begins, as a starting placeholder it overwrites with its real pointer (`gen.serve_chat`, `gen.serve_terminal` and `gen.update_ui_file` do).
- Writing the real pointer (or `gen.serve_chat` registering it) upgrades the host's starting claim to live; the renderer was showing it as starting and queueing the user's input until then. A cell-scoped path another live run already serves makes the host refuse the start with `surface_taken`.
- Derive the paths you serve from the same `resolve_opens` call (read your own main config's `fields:` beside your entrypoint), so the declaration and the code never disagree.

Chat layout belongs to your generator. Pass both `pointer_file=gen.run_path("chat/conversation.chat")` and `threads_dir=gen.run_path("chat")` to `gen.serve_chat`, even with `files=False`. With `files=False, broadcast=True` the run's inbox takes several viewers, so its approval cards reach the status pane.

Subprocess output can be staged with `gen.track_file(path)` without rewriting the file. It uses safe regular-file access and rejects `_runs/` paths; use `gen.update_ui_file` for declared UI files. In async code, wrap synchronous host-backed environment calls with `await asyncio.to_thread(...)` to keep the event loop available.

Use `gen.live_files(on_changed=callback, on_refused=callback)` for external-change notifications and refused-write corrections; callbacks receive a path, and refusals also receive the error. Own pushes never become external-change turns. When mirroring a workspace, pass generator-owned private paths explicitly with `gen.live_files(skip_globs=("private", "private/**"))`. Globs use case-sensitive `fnmatchcase` on cell-relative paths; suffix exclusions use the separate `exclude` argument. Give every concurrent action loop its surface source, for example `async for action in gen.actions(source=pointer):`, so it cannot consume clicks meant for another of the run's surfaces.

Declare account roles with semantic requirements:

```yaml
account_requirements:
  model:
    label: Text model
    kind: model
    capabilities: [text]
    protocols: [openai-compatible, openai-responses, anthropic, gemini]
    harnesses: [pydantic-ai]
    required: true
  git:
    label: Git account
    kind: git
    capabilities: [git]
    required: false
  issue_tracker:
    kind: custom
    custom_type: issue-tracker
    required: false
```

Key Manager stores a credential account once, with its models as subitems; account kinds are `model`, `git`, `account` and `custom`. Capabilities are a property of MODEL entries: a model entry carries its own `capabilities` list, and every other account carries the capabilities of its service. A model subitem chooses the model and optional harness (`metadata.harness`, default `pydantic-ai`). Bind the specific subitem, rather than its credential account, when the generator needs those details. A Git entry (kind `git`) is one of three services: `github` (github.com or a GitHub Enterprise server) and `gitlab` (gitlab.com or a self-managed instance) sign in with a personal access token (`api_key`) or an SSH key (`ssh_key`); `git`, any other Git server, signs in with an SSH key, a username and token (`basic`) or nothing (`none`, public HTTPS). Its `endpoint` is the web base where the repositories open in a browser (`https://github.com`, `https://gitlab.com`, a server's address; a `git` entry has one only with `basic`), and the API base is derived from it. Every Git entry carries the commit author as plain `metadata.author_name` and `metadata.author_email`, which every commit a run makes on the user's behalf uses; an SSH entry also carries the server's `metadata.known_hosts` lines unless the host keys ship with Collimate (github.com, gitlab.com). Nothing is exported to the environment: read the account with `_env_shared.git_account.git_account()`, which returns a `GitAccount` whose secret never appears in its repr. `collimate.git_hosts` holds the pure checks (host keys, known_hosts and private-key parsing, address rules). Which repositories a run clones or pushes is the generator's or runner's own setting, never Key Manager data. Custom entries declare `metadata.custom_type` and `metadata.field_schema` (`name` and `secret` for each field); ordinary values are in `metadata.fields`, and secret values are in the private `secrets` mapping.

Each declared role renders as its own row in the runner editor's Config tab: a key icon whose hover text is the requirement in words, the role's label with Required or Optional, and a dropdown of the entries that satisfy it (or "No valid account" with a button that opens Key Manager). Requirements filter those dropdowns and are checked again by the host. Declare the exact kinds, services, protocols, capabilities, authentication types, custom types and harnesses your generator supports. `kind`, `service`, `protocol`, `auth_type`, `custom_type`, and `harness` accept one string; their plural forms accept lists (`harnesses` is the plural of `harness`). Listed alternatives match any value; capabilities must all be present.

`gen.account('model')` returns the selected account snapshot: kind, service, protocol, endpoint, model, auth_type, capabilities, metadata and private secrets resolved from its credential account. Honor the chosen endpoint/protocol/model/harness and backend compatibility. Never select another provider, read ambient credentials, prompt for secrets during a run, or write secrets to an exportable cell. Rotated credentials are returned through `gen.commit_account(role, expected_revision=..., secrets=...)`.

For a selected account that needs a credential file, use `gen.materialize_account(role)` and release its lease after all consumers stop. The host maintains one temporary credential document shared by runs selecting that account generation, registers the run's permitted bindings, and collects refreshes with generation checks and writeback locking. Account edits and deletion cannot be undone by an older run. Put disposable harness homes and other private run state under workspace `.collrun/`, which is excluded from cell exports. Environment contexts stay within the workspace or staged app directories. Extra volumes may name only declared sockets or host-registered account lease paths; never pass an arbitrary host path. For an immutable policy document, `readonly_files` maps an absolute container destination to its text; the host creates and owns the read-only source and removes it at close.

To display a value derived from a selected entry and use it in conditional fields, declare its account source on a main-config field:

```yaml
- key: backend
  type: dropdown
  label: Agent
  account_role: model
  account_property: metadata.harness
  options: [pydantic-ai, codex]
  default: pydantic-ai
```

The editor displays the derived value read-only and updates dependent fields when the account selection changes. At run time, resolve it again from `gen.account`; a saved config value must never override the chosen account. Keep task choices such as prompt, run mode and approvals in main config.

The bundled Git generator (`git`) runs on one engine, `_env_shared/repositories.py`. Every git step runs in a temporary clone outside the cell, with a token only in an in-memory Basic header scoped to the account's base, or an SSH key in a 0600 file for one git command, never on argv. The cell never keeps `.git`: beside each repository folder `<folder>/` the cell root holds `<folder>.git-checkout.json`, recording `{about, remote, branch, commit}`; the built-in `git-checkout` handler checks it, and its skill tells an agent how to diff, log or blame against that commit from a temporary clone outside the cell. Clone / Update merges the remote's new commits into the folder's edits and changes nothing on a conflict. Push sends the edits as one commit on the recorded commit and never force-pushes. Symlinks, submodules, files over 20 MB and skipped folders stay in Git only. These need `git` 2.31 or later on this machine and internet: full; the generator declares `runs_on: local-machine`.

A generator works only on its own cell. The user chooses its runner and the cell where it runs.

Other generator manifest controls: `chat`, `daemon`, `environment`, `live_files_mode`, `runs_on`, `requires_docker` (every run provisions a container) and `requires_docker_if` (a condition over the main config's dropdown and checkbox values under which a run does; `collimate-lint` and the app flag a runner whose settings meet it without `docker: full`), `adopts_harness_grant` (the built-in Agent's opt-in: on the annotation surface the host and `collimate-lint` raise its runner's `docker` grant to the least its harness needs; any other generator's runner keeps its own grant), `takes_selected_file` / `takes_selected_file_if` and `opens` (above), `rlimits` (a `chat: true` generator gets no default CPU-time cap, because its budget restarts every turn while CPU time adds up over the session), `timeout_seconds` (a positive per-run budget in seconds, capped by the host), nonsecret `env_passthrough` (names host environment variables the generator needs, including any socket it will pass to environment placement), `enforces` and `ui_files` (above). A generator with `runs_on: local-machine` cannot use a containerized main loop.

`gen.grant()` is the run's effective grant, one attribute per axis (`gen.grant().docker`, `gen.grant().at_least("docker", "full")`); shape what the generator offers from it. The host enforces the grant's axes (see the API reference's Grants section), except `commands` (`deny` < `ask` < `allow`): whether an agent may run shell commands it chooses. Those run inside the harness the generator drives, where the host has no enforcement point, so a generator that runs an agent's commands declares `enforces: [commands]` and enforces the level itself — `collimate.permissions.effective_commands(gen.grant(), gen.can_ask())` gives the level (an `ask` nobody can answer becomes `deny`), and `await gen.ask(...)` shows the approval card. The runner editor offers `commands` only for a generator that declares it; `collimate-lint` warns when a runner of any other generator sets it.

```yaml
enforces: [commands]
```

Run `collimate-lint generators --strict` and the host-free generator tests before packaging.

## Messages from the user: the annotation context

A long run can take direction while it works. The user writes messages in the
cell's status pane, attaching selections from the files they view, and the run
reads them from its annotation context and answers in place. Subscribe once,
pull when the run can act on what waits, and post replies:

```python
import asyncio
import gen

async def run():
    wake = asyncio.Event()
    with gen.annotations.subscribe(lambda notice: wake.set()):
        while not gen.cancelled:
            gen.phase("awaiting_input", "waiting for a message")
            try:
                await asyncio.wait_for(wake.wait(), timeout=1.0)
            except TimeoutError:
                continue
            wake.clear()
            gen.phase("running", "answering")
            for message in await gen.annotations.pull():
                if message.reply_to is None:
                    await gen.annotations.post(f"Seen: {message.text[:60]}", reply_to=message.id)

if __name__ == '__main__':
    gen.main(run)
```

- Pull when the run can act: a pull marks every waiting message read and locks it for the user, so pull right before the work it starts, not on every notice.
- While nothing waits, park on `gen.phase("awaiting_input", ...)`: the host pauses the watchdog and shows the run as waiting.
- A dev run and a `test_generator` draft child have no context: `gen.annotations.available` is False, `pull()` returns `[]` and a subscription never fires.

## Options, Merge and Split

To produce candidates, write each into a separate folder in this cell, then
write a root `.options_stack` last with `update_file`:

```yaml
options:
  - name: Lean copy
    root: lean
    files: ["**"]
  - name: Full copy
    root: full
    files: ["**"]
rest: keep
```

`gen.options()` parses the declaration Split would select: root first, then
depth, then lexical path. Each option needs a name, optional literal folder
root, and nonempty globs. Only `*`, `**`, and `?` are special; other characters
are literal. Options may overlap. Matching files inside root lose that prefix;
outside-root matches retain paths relative to the declaration's folder. The
consumed definition is excluded. Check roots exist, matches are nonempty, paths
stay within the cell, and mapped destinations do not collide before writing it.

Split replaces the source with option cells; `rest: keep` retains unselected
files in a new source version and inserts options after it. `drop` removes the
source; `each` copies unselected files to each option and removes the source.
Run folders and the cell's annotation messages stay with the source. Merge
combines two or more standard cells and writes their root options declaration.
Imported options create new logical cells. Local unchanged options can restore
their absent original logical cells when the recorded version is still the tip;
changed options create new cells.

With `async with gen.live_files() as mirror`, await `mirror.flush()` before
closing the correction surface. It awaits final push acknowledgement and raises
read, transport or partial-refusal errors, including prior background failures.
Then await `gen.commit()` for additionally staged output. Finish only after
acknowledgement; a refused final write must be corrected while a turn can still
run. No handler validation API is required for this current file format.

### Test a draft in the current cell

An agent with command permission can call `test_generator(path, settings, prompt)`
on the package's `.generator` file. The SDK's `collimate.testing.forward` relays
only cell output, file reads, reveals, client capabilities and handler tools to
the parent; the child has no annotation context (`gen.annotations.available`
is False). The child runs with `uv` and its declared dependencies in the agent's
command environment, with a private prompt/config directory and the parent's
identity/grant. `commands: ask` requires a card; `deny` offers no test tool.
Surface, session, terminal, environment and account operations are refused.
Use Add to Capabilities and a real runner to test those capabilities. See
[Testing generators](testing-generators.md#test-a-draft-from-an-agent-command).
