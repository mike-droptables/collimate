# Authoring best practices — lessons from the packaged set

The generators and renderers that ship with the SDK (the shipped Agent
cell's `agent` generator, the `launch` and `web`
generators, their runners, and the shipped renderers) encode a house
style. This file distills it into rules. When scaffolding new work, follow
these on top of the contracts in
`how-to-write-a-generator.md` / `how-to-write-a-renderer.md`.

The meta-rule running through everything: **defaults must make the artifact
work with zero edits; every degradation is logged with the user-facing fix in
the message; every non-obvious line carries the failure it prevents as a
comment; and shared behavior is hoisted into one helper the moment a second
consumer appears.**

## Manifests

- **One cell per run.** Read its files with `gen.list_files()` and
  `gen.read_file(path)`. A merged input carries an `.options_stack`;
  `gen.options()` returns its selected declaration. Preserve option names and
  use the shared options mapper when mapping files. Produce candidates in
  separate folders, write the root declaration last, and wait for acknowledged
  output before success. The user presses Split; this run writes only its cell.
- **Declare lifecycle in the manifest, not in code**: `chat: true` for
  conversational generators, `daemon: true` for long-lived servers/terminals
  (`web`, `launch`'s app mode). Every run — however long-lived — ends the
  same way, with Stop in its view in the cell's status pane and in the top
  bar's runs menu (the cell's status light opens the pane; several runs can
  share a cell, one row each).
  When a run stops, the host clears its run folder: nothing a run leaves in
  `_runs/<label>/` outlives it.
- **`live_files_mode` is a deliberate three-way choice**: omit it
  (`"none"`, the default) when the run never needs the cell's files
  mirrored mid-run; `"auto"` when zero sync code is wanted; `"manual"`
  when sync needs customizing (`launch`'s app mode skips
  `.git`/`node_modules` noise; the Agent generator skips its
  private `_agent/` folder, turns refused writes into correction turns and
  folds outside edits into its next turn).
- **Declare the grant axes the generator enforces itself.** The host has no
  enforcement point for `commands` (the API reference's Grants section says
  what it enforces, and where); a generator that runs an agent's shell
  commands declares `enforces: [commands]` and enforces the level through
  `collimate.permissions.effective_commands(gen.grant(), gen.can_ask())`.
- **Named configs belong to generators.** Each is a complete settings/file/security snapshot. The default is canonical. Runner search tags never select behavior.
- **`prompt_intent` is a placeholder written as an instruction with an
  example** ("URL to open (e.g. https://example.com)"). Add
  `prompt_required: true` only when the prompt is truly mandatory; otherwise
  say what blank does ("leave blank for an empty chat"). When the prompt only
  belongs to some selections, gate it with the conditional forms
  `prompt_show_if` / `prompt_required_if` (set_yaml conditions over the main
  config's values) rather than asking for a prompt the run would ignore —
  `prompt_show_if: {session: oneshot}` hides the box for every other shape.
- **`summary` is a capability paragraph for the Generators list in Capabilities, not marketing** —
  name the mechanism, the presets, and the input and output contract.
- **Host-environment needs are manifest keys with a comment naming the
  failure they prevent**: `env_passthrough: [SSH_AUTH_SOCK]` ("without this
  the ssh-agent path silently no-ops").
- List every user-editable config file in `draft_files`.

## Config handling

- **Every field defaults such that the generator runs unedited**, and code
  re-defaults anyway: `(cfg.get("backend") or "pydantic-ai").strip().lower()`.
- **Never bare `int()`/`bool()` on config** — use defensive coercion with a
  default and clamping:

  ```python
  def _int(raw, default: int) -> int:
      try:
          n = int(raw)
          return n if n > 0 else default
      except (TypeError, ValueError):
          return default
  ```

- **Reject invalid explicit values.** Honor the selected account's endpoint, protocol and model. Missing or incompatible accounts make the runner unready; do not substitute a different provider or model.
- **`.set_yaml` field descriptions teach the failure mode and the fix**, not
  just the meaning ("the tag must already be built/pulled on the host").
  Hide expert knobs with `advanced: true`.

## Lifecycle and UX

- **The universal skeleton**: `async def run(): ...` +
  `if __name__ == "__main__": gen.main(run)`. Daemons park on
  `await gen.wait_until_cancelled()` inside `try/finally` teardown.
- **Cell names belong to the user**: `await gen.commit(name=...)` may name
  only a cell the host created for this run; the host ignores it for an
  existing cell. Launch proposes the saved runner's name, or `Launch` when
  absent. Show ongoing work through the run's status and logs: both appear
  under Log in the cell's status pane (a run's drilldown shows its own lines;
  All merges every run on the cell by time).
- **Two distinct progress channels**: `gen.phase(name, message)` for the
  machine state and the human-readable progress beside it (set the same
  phase again to change only the message), `gen.log()` for diagnostics
  (prefix log lines `[gen-id]`).
- **Finish the cell well**: `await gen.commit()` plus a final
  `gen.log()` carrying the facts ("Cloned <url> · 128 files", "ran (exit 0);
  serving :8000"). Reveal the file the user should see first with
  `await gen.reveal_file(path)` — a run never pins its cell; the pin is the
  user's.
- **User errors produce `error.md`, not a stack trace**: on bad input write a
  title + the fix ("Paste a repo URL into the runner's prompt"), call
  `gen.log("Failed: <reason>")`, and `return`. Reserve `raise` for real bugs.
- **Serve UIs through pointer files in the run folder** (`.terminal`,
  `.website`, `.webview` JSON pointers written with `gen.update_ui_file(name, ...)`),
  then reveal them with `gen.reveal_file`. Declare every UI file type in the
  manifest's `ui_files` — an undeclared one is refused by the SDK and the
  host alike. A pointer outside the run folder is refused (`ValueError` from
  `gen.serve_chat` / `gen.serve_terminal`): the
  folder locks those transient files and clears them at Stop.
- **The run folder is for UI files only.** Work output — edits, clones,
  summaries, captures, attachments — goes anywhere else in the
  cell with `gen.update_file`, exactly as it would with no run folder;
  `gen.update_file` refuses every `_runs/` path. For terminals use
  `gen.serve_terminal(container_id, command, pointer=...)` — it writes and
  reveals the canonical pointer, and stops the PTY and deletes the pointer on
  exit, so the pointer shape can't drift and teardown can't be forgotten.
  Register ports with `gen.serve_port` and gate on readiness (poll the port
  with a deadline and a clear timeout message) — never reveal a dead iframe.
  A pointer outlives nothing it points at: delete it when its session ends
  (the host sweeps the folder at Stop as the backstop).
- **Several runs can share a cell.** Write the run's user interface inside
  its own folder; everything else in the cell is shared and may collide with
  another run's work. Another run's folder is read-only: read it freely;
  talk to the user through `gen.annotations`, not through files.
- **Let the user steer a long run through its annotation context**: subscribe
  once, pull when the run can act, answer with
  `await gen.annotations.post(text, reply_to=message.id)`, and park on
  `awaiting_input` while nothing waits.
- **Multiple results**: write result files and folders into the run's own cell.

## Containers and credentials

- **Self-contained images**: ship the `Dockerfile` in the cell, build on
  first use, and tag by content (`_env_shared.derive_image_tag`) so a
  Dockerfile edit is a new tag the host builds. Stream build steps into
  `gen.phase`'s message. Bake dependencies into the image — runtime installs are
  unreliable.
- **Shared machinery lives in `_*` helper libraries** (`_env_shared`,
  `_agent_shared`, `_web_shared`), imported via the two-line `sys.path`
  shim; the SDK itself stays Docker-free. Name each one under `shared:` in
  the cell's `cell.yaml`: admission copies it into the cell from the
  installed helper set, whatever the cell's source (a suggestion, a zip,
  another channel), and refuses a name the set does not hold. Never ship
  your own copy.
- **Bind-mount the cell workspace read-write at a fixed path**, match uids,
  label containers with the run id, stop with SIGTERM before force-remove so
  mounted writes aren't killed mid-flight, and make `close()` idempotent.
- **Placement stays inside the run.** Workspace and build contexts resolve within the run workspace or staged application tree. Extra volumes name only actual sockets declared by `env_passthrough` or account lease paths registered by the host for this run. Immutable policy text uses `readonly_files` (container destination to text), whose source and lifecycle the host owns. Stage disposable private files under workspace `.collrun/`; it is excluded from public cell files and keeps owner-only permissions.
- **Secrets come from explicit account roles.** Use `gen.account(role)`. Key Manager owns setup and login; failed credentials stop the run with an actionable error. Never prompt for a replacement inside a generator, read the user's CLI home, or put secrets into exported cell files. Pass credentials through private files or process environments, not argv. Return rotated credentials through revision-checked `gen.commit_account`. For a materialized account file, hold the host lease until every consumer stops, then release it for generation-checked collection; concurrent consumers of one credential generation share its host-managed document, and each release revokes that run’s binding authority.

## Renderers

- **Native vs iframe is chosen by state, and the manifest comments the
  choice.** Anything holding a live WebSocket must be `embed: "native"` —
  the iframe pool parks offscreen iframes and a remount would double-connect
  (terminal, assistant chat). Heavy third-party editors go `embed: "iframe"`
  (monaco, excalidraw).
- **Shadow DOM styling**: inline styles or an injected `<style>` block —
  host classes never reach the shadow root. Style on the skin tokens
  (`tokens.color.*`, `var(--sheet)`, `var(--ink)`) so every skin works, dark
  and light; write no colour literal and no `var(--token, #hex)` fallback.
  The exceptions are documented in the renderer guide: an authored or guest
  page keeps its own colours, and a terminal's ANSI palette is declared once
  per scheme while its ground stays `var(--code-bg)`.
- **Use the SDK hooks**: `defineRenderer`, `useCollimateFile()`, `useTheme`,
  and `useVisibility`. Editors publish every serialized document change with
  `saveContent`; the host journals locally and coalesces server requests.
  Respect `isReadOnly`.
- **`isReadOnly` is the hard lock.** The host returns true when saving is
  structurally impossible (diff mode, a read-only cell) — every `reportChange` is
  discarded, so hide *all* mutating affordances and never offer an "edit
  anyway" escape, which would silently eat the user's edits.
- **Pointer files are parsed defensively**: validate JSON field-by-field
  with fallbacks (the website renderer treats non-JSON content as a plain
  URL) and preserve fields you don't own when writing back.
- **`supports_diff: true` only where a text diff is meaningful**; comment
  why when it isn't (excalidraw's nested JSON).
- **Sanitize rendered content**: native renderers have SDK API access, so
  block `javascript:`/`data:` URLs in anything user-authored (markdown).
- **Always validate the renderer definition before calling it done.**
  The accepted renderer handler bundles the package and checks its export statically.
- **Never let an unfocused cell see input.** A native renderer lives in
  the main document: a bare `window.addEventListener('keydown', ...)`
  hears (and can `preventDefault`) every keystroke in the app, even when
  your cell is not selected. Attach input listeners to your own root
  element, or gate a window-level listener on `useHostFocus(api)` so it
  is inert while the cell is unfocused. The host also enforces this:
  renderer-registered input listeners are force-gated on cell focus, so an
  ungated listener is silently dead while the cell is unselected — gate it
  yourself to make the behavior explicit.

## Code style

- Every entrypoint starts with a **PEP-723 header** listing only the deps the
  generator itself imports; heavy imports (`docker`) are deferred into the
  functions that use them.
- **Module docstrings explain the design** — the flow, the modes, the why,
  not the history — and not the API. Comments state the current rule and the
  bug a line prevents.
- Helper functions are small, defensive, and named for what they normalize
  (`_int`, `_as_string_list`, `_scrub`).
- **Test before done**: run the generator (`run_generator` /
  `collimate.testing.run_generator`), compile the renderer, and lint
  manifests with `collimate-lint`.
