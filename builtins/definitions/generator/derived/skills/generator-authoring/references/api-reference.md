# SDK API reference

Import `gen` inside a generator. It exposes the host-scoped API while `gen.main(run)` owns startup, cancellation and final output flushing, and exits with status 1 when the run fails or is stopped.

| API | Contract |
| --- | --- |
| `gen.prompt` | Saved runner prompt |
| `gen.selected_file` | The cell-relative path of the file the user had selected when they started the run, or `None`. Set only for a generator that declares `takes_selected_file` (or a matching `takes_selected_file_if`); the generator reads the content itself (`gen.read_file`) |
| `gen.config(path)` | Saved values for the generator's main config |
| `gen.list_files()`, `gen.read_file(path)` | Files in the run's own cell |
| `gen.options()` | Selected `.options_stack` as `{path, options, rest}`, or `None` |
| `gen.config_folder(name)`, `gen.config_folder_path(name)`, `gen.config_folders()` | Runner-supplied supplemental folder content |
| `gen.update_file(path, content)`, `gen.delete_file(path)` | Stage output changes anywhere in the cell outside `_runs/`; every `_runs/` path, the run's own folder included, raises `PermissionError` at once (see [Run folders](#run-folders)) |
| `gen.track_file(path, category="file", max_bytes=None)` | Track a subprocess-written regular workspace file for the next flush without rewriting its bytes or changing its mtime. Uses safe no-follow access; refuses every `_runs/` path. An optional byte limit raises `ValueError` before tracking an oversized file |
| `gen.update_ui_file(name, content)` | Create or update the run's UI file `<folder>/<name>` (`name` relative to the run folder, or the full cell-relative path inside this run’s folder; subfolders allowed); its suffix must be one the manifest declares in `ui_files`, else `ValueError`. Staged like any write; returns the cell-relative path |
| `gen.delete_ui_file(name, log=False)` | Remove the run's UI file using its folder-relative name or full cell-relative path with the next flush; `log=True` keeps its last content in the run's Logs under the same path |
| `gen.list_ui_files()` | The run's UI files the SDK knows about, as folder-relative names |
| `await gen.commit(name=...)` | A supplied name may name only a cell the host created for this run; existing-cell names remain the user’s. Flush output changes, one flush at a time; writes still staged when `run()` returns are flushed for you, and the run fails when that final flush fails (a partial refusal is logged and the run stands). `gen.PartialRefusal` (a `PermissionError`) names every refused path in `paths`, with its handler verifier error in `errors` when applicable (the rest is committed). A failed host commit raises `IPCError` with the host’s error and restores writes and deletions for retry, preserving newer changes staged meanwhile; a transport timeout requires ending the run as described below |
| `gen.finish(note=None, outcome=None)` | Record the run's own verdict for its end: a one-line `note` (whitespace collapsed, at most 300 characters; not a string raises `TypeError`) and `outcome` `"pass"` or `"fail"` (else `ValueError`). Does not end the run: `gen.main` sends it when `run()` returns or raises; the last call wins |
| `await gen.reveal_file(path)` | Show a cell file to the user as a tab of the run's cell; flushes `path` first when it is still staged |
| `gen.read_file(path)`, `gen.list_files()`, `gen.has_file(path)` | Current output workspace. Text reads back exactly as written (CRLF stays CRLF); `has_file` and `read_file` follow no symlink |
| `gen.account(role)` | Explicitly bound private account snapshot |
| `gen.commit_account(role, expected_revision=..., secrets=...)` | Compare-and-swap credential refresh. A run replaces only fields the account's `secrets` already hold (and `expires_at` and `token_type` unless the account is `custom`), each with a nonempty string; a new field, a `None` or an empty or non-string value raises `RuntimeError` and writes nothing |
| `gen.grant()` | The run's effective grant, a frozen `Grant` (see [Grants](#grants)) |
| `gen.run_info()` | `RunInfo(run_id, channel, cell_id, runner_id, runner_name, label, folder)`: who this run is to the host (see [Run folders](#run-folders)) |
| `gen.run_path(rel)` | `rel` inside the run's folder: `"<folder>/<rel>"` (the path a pointer is written and revealed by) |
| `gen.annotations` | The run's annotation context: the user's messages to the run and its replies (see [Annotations](#annotations)) |
| `gen.serve_port(port, ...)` | Register a port and wait for host acceptance; host refusal raises `IPCNakError` at this call, while a denied grant raises `PermissionError`. It does not publish a refused port or defer the error to a later request |
| `gen.proxy_url(port)` | The app-origin path a browser reaches a port this run serves (after `gen.serve_port`) |
| `gen.packages()` | Saved upstream package requests |
| `await gen.ask(title, kind=..., detail=..., options=..., timeout=600)` | Show one approval card and wait; the chosen `option_id`, or `None` when refused (see [Approval cards](#approval-cards)) |
| `gen.can_ask()` | Whether a human can answer approval cards in this run |
| `gen.place_environment(spec)` | Ask the host to place a container for this run; `(env_id, container_id)` |
| `gen.exec_environment(env_id, argv, cwd=..., env=..., timeout=..., user=..., detach=...)` | Run a command in it; `(rc, output)`, `(-1, error)` when the host could not |
| `gen.expose_environment_port(env_id, port)`, `gen.close_environment(env_id)` | Forward a container port to a host port; tear the environment down |

Synchronous host-backed calls such as `gen.place_environment`, `gen.exec_environment` and `gen.terminal_status` wait for IPC on the caller’s thread. Async generator code should call them with `await asyncio.to_thread(...)` so streaming and other tasks can continue.

`gen.snapshot()` shares a stat-keyed digest cache with the live mirror and exec tracking: unchanged regular files reuse their digest; inode, size, nanosecond mtime and ctime changes invalidate it. Live reconciliation fetches cell hashes first and requests only changed file bodies.

A runner is a complete saved invocation chosen by the user.

`GeneratorResult` contains `ok`, `files`, `error`, logs, `phases` (every `(phase, message)` the run set, in order) and endpoints, plus the `note` and `outcome` that `gen.finish` recorded (`None` when it was not called), and `annotations`, the run's annotation context at its end as `gen.Annotation` values (host-free harness only); `raise_for_status()` raises a failed result. `result.output_text(path)` reads the caller-declared text path (empty when absent or binary), and `result.structured(path, model=None)` parses JSON from the declared path (see [testing](testing-generators.md)). Neither assumes an agent-specific output location.

Interactive and runtime APIs: `gen.serve_chat`, `gen.ChatTurn`, `gen.history`, `gen.Message`, `gen.annotations`, `gen.serve_terminal`, `gen.start_terminal`, `gen.serve_port`, `gen.live_files`, `gen.log`, `gen.phase` (its message is the run's status: set the same phase again to change it), and the cancellation helpers. Account setup belongs in Key Manager, before the run. The environment-plane calls above are what `_env_shared.DockerEnvironment` wraps; the host gates them at `docker: full`. `gen.close_environment(env_id)` returns the host’s `{ok, error}` acknowledgement and raises an IPC error when no acknowledgement arrives. `DockerEnvironment.close()` returns a cached `{ok, error}` status, logs a failed acknowledgement or transport failure to the run events, and makes only one close request. The host’s run teardown remains the cleanup backstop.

`gen.serve_chat(on_turn, pointer_file=..., threads_dir=...)` requires both paths explicitly: it writes and reveals that `.chat` pointer and keeps each thread's transcript in `<threads_dir>/<id>.json`. Both paths must lie inside the run folder, else `ValueError`. Both are UI files, so the manifest's `ui_files` must declare `.chat` and `.json` (else `ValueError` before the session starts). `gen.serve_chat(on_turn, pointer_file=..., threads_dir=..., files=False)` still registers the run's inbox, one per run: user messages, interrupts, approval answers and config changes arrive there, and the run's own queued turns run on it. It writes no pointer and no thread file; the transcript lives in memory for the run (`gen.history` reads it). Use it when the run's face is elsewhere (its annotation context, a terminal). `broadcast=True` (only with `files=False`; with files it raises `ValueError`, since the `.chat` surface is single-attach) lets several viewers attach to the inbox, so approval cards reach every status-pane view of the run. An `interrupt` command names the turn it stops by `turn_id` (a broadcast inbox viewer's Stop) or by `thread_id` (the chat composer); one naming neither is dropped.

The chat transcript follows the connected harness's conversation. The renderer sends new messages and interrupts; past messages are read-only. Unsupported commands receive a `refused` frame with `op`, `thread_id`, and a human-readable `message` and never reach the harness.

`gen.serve_terminal(container_id, command, pointer=...)` allocates a PTY running `command` in one of the run's containers, or on this machine with `container_id=None` (the host form: `env` is the program's whole environment, `cwd` a directory of the run's workspace, and the program leads its own session with the PTY as its controlling terminal; the host refuses it unless the run's grant allows execution here, `docker: none` or `full` with `commands` other than `deny`, and, for a run bound to a harness, its placement puts the harness's CLI on this machine). It writes and reveals its `.terminal` pointer (which must lie inside the run folder and be a type the manifest's `ui_files` declares, else `ValueError` before the PTY starts) as a UI file and, when the block exits, stops the PTY and deletes the pointer. Use `async with gen.serve_terminal(...) as session:` with `await gen.wait_terminal_exit(session["terminal_id"])` to end when the program exits. The wait polls host status off the event loop (default `poll=1.0` seconds), returning its exit code, or `None` when the terminal disappears without a code or the run receives Stop; host errors propagate. The wait does not stop the PTY itself—the context owns cleanup. Launch ends terminal-mode runs this way and reports a nonzero code as a failed run. Keep `wait_until_cancelled()` for daemons that intentionally outlive an individual process.

`gen.require_ui_types(context, *paths)` checks own-run UI types before a multi-file initialization.

## Run folders

User saves remain authoritative: the server validates current handled files after each accepted file revision, without delaying or refusing the save. Package sibling edits also revalidate the package definition. The editor reads advisory per-file results from `GET /api/channels/{channel}/cells/{cell_id}/validation`: `{cell_id, source_version_id, source_revision, pending, verdicts}`, where `verdicts` maps paths to `{valid, error?}`. Changed/deleted files or an accepted-catalog change invalidate older results; `pending` responses contain no stale verdicts. Results are bounded in server memory and revalidated after restart. These results are not approval tokens and do not replace a run's write gate.

Several runs can work in one cell at once. Each has a label, `<runner_id>-<run_id[:8]>`, and a run folder, `_runs/<label>/` — `gen.run_info().label` and `.folder`. Every file in a live run's folder is its **locked file**: the files that are the run's user interface (its pointers and its transcripts). The user cannot delete, move or copy them out of the cell while the run lives, and other runs read them but never write them.

**The run folder is for UI surface files ONLY.** It is not a working directory and never the preferred place for anything else: a run's work output (edits to the cell's files, cloned repos, summaries, captures, attachments) goes anywhere else in the cell exactly as before, and accepted handler verifiers check typed writes against the post-flush package. The host refuses invalid typed files (`handler_invalid`, with the verifier's error), writes into another run's `_runs/<label>/` folder, the run's own card, and files in the run's own folder whose type the manifest's `ui_files` does not declare. A refused definition never blocks its ordinary code siblings.

- **UI files are created only through one explicit API, and only for declared types.** The generator manifest lists the filename suffixes of its UI files, `ui_files: [".chat", ".json"]`. `gen.update_ui_file(name, content)` accepts a folder-relative name or this run's full cell-relative path and raises `ValueError` for a suffix the manifest does not declare; `gen.delete_ui_file(name)` removes one (`log=True` keeps its last content in the run's Logs); `gen.list_ui_files()` lists them. `gen.update_file` / `gen.delete_file` raise `PermissionError` for every `_runs/` path, the run's own folder included.
- The host holds the same rule: it passes the declared list to the run (`COLLIMATE_UI_FILES`) and refuses an own-folder file of any other type, so it holds for a harness that writes straight into the workspace too. `gen.commit()` raises `gen.PartialRefusal` naming whatever the host refused.
- The host writes the **run card**, `_runs/<label>/<label>.run`, before the run starts, so a run always has at least one locked file, and removes the folder when the run ends — whatever the run did not remove itself, however it ended. Nobody else writes or deletes the card.
- In the same version as the card the host writes a **starting placeholder** for each file the manifest's `opens` resolves to (`collimate.surfaces.resolve_opens`): `{"starting": true, "label": <runner name>}` for a pointer type (`.chat`, `.terminal`, `.webview`, `.website`); any other path gets none and is claimed as it is. It claims those paths as starting surfaces and reveals the first, so the surface shows before the process spawns. The run finds them in its workspace and writes its real pointer over a placeholder.
- `gen.serve_chat` and `gen.serve_terminal` require own-run paths and declared UI types.
- Nothing pins a run's cell. A run shows a file with `await gen.reveal_file(path)`: the clients open it as a tab of the cell, activated when the active tab is empty or already one of this run's files, else beside it. Flush first, or let `gen.reveal_file` flush for you.
- `gen.live_files(skip_globs=(), on_refused=None, on_changed=None)` pulls the run's own folder from the cell and pushes it back only for the declared UI file types; a file of any other type dropped there is removed again (`[live] restored <path> (undeclared ui type)`). The card and every other run's folder are read-only in the workspace: never pushed, and a local change is overwritten with the cell's copy (`[live] restored <path> …` in the run log).
- Mirror `skip_globs` patterns match cell-relative paths with case-sensitive `fnmatchcase`; `exclude` remains a tuple of suffixes. The default skips no generator-specific directory: an Agent caller declares `("_agent", "_agent/**", ".pydantic-ai-harness", ".pydantic-ai-harness/**")` itself (its run records, and Pydantic AI Harness's own shell logs in the workspace). Snapshot and mirror directory exclusions share `collimate.fs.WORKSPACE_SKIP_DIRS`; `gen.list_files()` remains exhaustive apart from SDK-private and log files. A mirror logs its third consecutive reconciliation failure once, then logs recovery when reconciliation succeeds.
- Cell files outside an active served band are shared by runs and may collide; a user who wants runs kept apart starts one in a new cell. `on_changed(path)` reports only pulled external changes after they land locally, including deletion; own pushes and echoes do not fire it. `on_refused(path, error)` preserves refused local bytes so the generator can request a bounded correction.

### File persistence and concurrent writers

The host gives each run the file revisions frozen when its workspace is staged.
`gen.commit()` publishes changed paths conditionally against those revisions.
Each flush has a durable operation ID and writer sequence. Its artifact bytes
are frozen before IPC; a lost acknowledgement replays the same operation before
newer workspace edits are sent. Reading a hash inventory does not rebase edits.
The live mirror pins body reads to the inventory revision it observed.

A competing accepted revision preserves the run's candidate and raises
`gen.FileConflictError`; local staging remains available. Review and resolve
the competing versions explicitly in the editor. A timeout or storage failure
also raises and retains staging. The browser editor journals every snapshot
locally and reports browser durability separately from server acknowledgement.

## Annotations

The run's annotation context is a message store between the user and the
run: the user writes messages in the cell's status pane (queued, or interrupt
to cut the current turn short), and the run reads them and answers.

| API | Contract |
| --- | --- |
| `gen.annotations.available` | True for a host run; False in a dev run and in a `test_generator` draft child, which have no context |
| `gen.annotations.subscribe(on_new, on_interrupt=None)` | Watch the context and return a subscription (`close()` it, or use it as a context manager). Callbacks are plain synchronous callables taking the current `gen.AnnotationNotice`, run on the event loop's thread: `on_new` fires on a poll whose context revision changed while unread messages wait, and `on_interrupt` fires first when `interrupt_seq` passes the highest one already dispatched or answered by a pull (no interrupt fires while a pull is in flight; the pull says which it answers). Call it from the run's event loop. Subscriptions share one poller (every 0.25 s), which stops at the last `close()`, at Stop, when the context is sealed, or after a transport failure (`IPCDisconnectedError`, `IPCTimeoutError`; logged once); a poll the host refuses is logged and retried, backing off to 5 s. A poller started after the last one closed carries on from what it saw. A raising callback is logged and watching continues; a coroutine function raises `TypeError`. Without a context the subscription never fires |
| `await gen.annotations.pull()` | Every waiting message (interrupt-mode first, then queue order; an unread root's unread replies follow it), now read and locked. Exactly once: a pull whose answer was lost is replayed before anything newer is pulled, and a cancelled pull keeps its batch for the next one. `[]` without a context |
| `await gen.annotations.post(text, reply_to=None)` | Post markdown from this run, tagged with its label, and return the new id; `reply_to` names a top-level message (replies are one level deep). Empty text, more than 64 KiB of UTF-8, or a `reply_to` naming a reply raise `ValueError`; an unknown `reply_to` raises `LookupError`; a sealed context (the run is ending) raises `RuntimeError`. A post the host may have taken is replayed under its id, never doubled. Without a context it raises `LookupError` |
| `await gen.annotations.list()` | The whole context the run sees (its prompt, read and unread messages, its own posts) in context order; `[]` without a context |
| `gen.annotations.notice` | The newest `AnnotationNotice(rev, unread, interrupt_seq)` the run has seen: the last poll's, or the last pull's when that one is newer. It never goes back to an older revision, so a message posted while a pull was in flight still counts as unread |
| `gen.annotations.pull_notice` | The context's `AnnotationNotice` as the last pull left it (`AnnotationNotice(0, 0, 0)` before the first): its `interrupt_seq` counts every interrupt that pull's batch answers, and its `unread` what it left waiting (a pull takes at most 64 messages) |

A message is a `gen.Annotation(id, text, author, author_label, kind,
reply_to, mode, created_at, read_at, context, refs)`: `author` is `"user"` or
`"run"` (with the posting run's `author_label`), `kind` is `"message"` or
`"prompt"`, `mode` is `"queued"` or `"interrupt"`, the times are UTC
`YYYY-MM-DD HH:MM:SS` (`read_at` is `None` until pulled), `context` holds the
`gen.ContextItem(path, renderer_id, label, text, anchor)` values the user
attached from a renderer, and `refs` the cell paths the text links. The host
seeds a run's non-empty prompt as a read `kind="prompt"` message. Messages the
user holds below the queue bar are invisible to the run. The run is
"listening" from its first poll. An interrupt-mode message moves to the front
of the queue and advances `interrupt_seq`. A run reads and posts only its own
context. At run end the host seals it and writes it to the run's Logs as
`annotations.json`; `gen.update_file` and `gen.track_file` refuse `logs.txt`
and `annotations.json` as log paths. While it waits without `gen.serve_chat`,
set `gen.phase("awaiting_input", ...)` so the watchdog pauses.

## The rest of the surface

- Types a generator receives: `GeneratorResult`, `Endpoint`, `RunInfo`, `DraftFileSchema`, `ConfigField`, `ConfigValidation`, `Annotation`, `ContextItem` and `AnnotationNotice`.
- Errors: `Cancelled` (the user stopped the run), `PartialRefusal` (a `PermissionError` from a flush the host committed only in part: `paths` and `errors` name the refused writes; a NAK's `PermissionError` committed nothing), and `IPCError` with its kinds `IPCNakError`, `IPCTimeoutError` and `IPCDisconnectedError`. Frames over the host limit raise `ValueError` before being sent. A transport timeout marks the connection desynchronized and refuses later requests; end the run instead of retrying on that connection. An explicit failed-commit response or NAK does not desynchronize it. Output files are read from the workspace. A hosted run that cannot reach the IPC port stops before `run()` starts.
- Credential leases: `gen.materialize_account(role)` returns the selected account's host-managed temporary credential path and lease. Concurrent runs selecting the same credential generation share that document. The manager registers each run's authorized lease paths internally; callers may bind only those paths or their registered descendants. Stop every consumer before `gen.release_account(role, lease_id)`: release collects refreshes with generation checks and writeback locking, revokes that run's binding authority, and removes temporary data after the last holder releases. Failed collection retains the document for a later host sweep; account edits/deletions cannot be overwritten by stale generations.
- Client and user interaction: `gen.client_caps()` (what the client driving the app can do), `gen.progress(current, total)`, `gen.actions(source="", poll=1.0)`, `gen.wait_for_action(timeout=None, source="")` and `gen.wait_for_action_event(source=..., action_id=..., timeout=...)` (renderer button clicks), and `gen.wait_until_cancelled()` (park a daemon until Stop). In concurrent action loops, pass the surface path as `source`. An empty source matches any queued action.
- Environment placement: `gen.place_environment(spec)` accepts `workspace`, `build_context` and `base_context` only within this run's workspace or staged application tree, after resolving symlinks. Generator-supplied `volumes` are limited to actual manifest-declared sockets and the account lease paths registered by the host for this run. `readonly_files` maps absolute container paths to text: the host creates private immutable files outside the workspace and mounts them read-only, cleaning them on placement failure or environment close. It accepts no host source path. This preserves the read-only command-policy binding without allowing arbitrary host paths. Disposable harness homes stay in workspace `.collrun/` and reach their container locations through the workspace mount.
- Terminals and the filesystem: `gen.terminal_status(terminal_id)` and `gen.stop_terminal(terminal_id)` for a terminal this run serves, and `gen.workspace_path()` (the workspace's real path, for a subprocess cwd or a mount).

## Grants

`gen.grant()` returns a frozen `collimate.permissions.Grant` with `update_packages`, `docker`, `internet`, `internet_allow` and `commands`. `.at_least(axis, level)` compares the ladders: docker `none` < `limited` < `full`; internet `none` < `proxy` < `full`; commands `deny` < `ask` < `allow`.

A grant has two layers. A saved runner's `permissions` block is the manifest layer: the host fills every axis the block omits with that axis's default (`commands: deny`, `internet: full`, `update_packages: true`, every other axis at its lowest level). What reaches the run is the environment layer, `COLLIMATE_PERMISSIONS`: the host always sends every axis, `{}` there is an unrestricted dev run (every axis at its top), and `Grant.from_dict` floors an omitted axis or an unknown value to the lowest level. A host run with no grant, a value that is not a JSON object, or a grant naming an axis the SDK does not know stops the run. The host enforces `docker` at placement and at the environment-plane ops, and `update_packages` for everything it builds or pulls. It enforces `internet` and `internet_allow` only for a `docker: limited` run, whose container sits behind its egress proxy: a `docker: none` or `full` run is a native process on the host network, and its IPC gates bind what it asks the host for, not what the process can reach on its own. `commands` has no host enforcement point, because the shell commands an agent chooses run inside the harness or sandbox its generator drives: a generator that honours the axis declares `enforces: [commands]` in its manifest and enforces it itself. A generator also honours `update_packages` for the registry lookups it makes itself.

`collimate.permissions.effective_commands(grant, can_ask)` returns the level to enforce and a warning when it differs: `ask` becomes `deny` where nobody can answer (`gen.can_ask()` is False).

## Approval cards

`await gen.ask(title, kind=..., detail=None, options=None, timeout=600)` shows one card through the run's inbox and waits. `kind` names what is being approved (`execute`, `edit`, `fetch` …), `detail` is the text the card shows in full (a command line, a path), and `options` are `{"option_id", "name", "kind"}` dicts whose `kind` is `allow_once`, `allow_always`, `reject_once` or `reject_always`; `None` offers Allow (`allow_once`) and Reject (`reject_once`). `timeout` is a finite, positive number of seconds; malformed options or a bad timeout raise `ValueError`. It returns the chosen `option_id`, or `None` when the card is refused: nobody can answer, the user dismissed it, the answer named an option never offered, the run stopped, the chat session ended, or `timeout` seconds passed. Cancelling the awaiting task closes the card as refused.

A card is answered through the run's inbox, the session `gen.serve_chat` serves. It shows on the `.chat` surface (`files=True`) or on every viewer of a broadcast inbox (`files=False, broadcast=True`); with neither — a transport-only inbox without broadcast, or no chat session served — `gen.ask` refuses at once without a card. Ask from a chat turn (or a task running beside one).

`gen.can_ask()` is False without a host; `gen.ask` then refuses without showing a card.

While a card waits, the run's phase is `awaiting_approval`: the host pauses the wallclock watchdog without restarting the turn's budget. A phase the run sets meanwhile is held, and the newest one is shown when the last open card closes.

The card wire spells the option id `option_id` everywhere:

| Frame | Shape |
| --- | --- |
| Card (stream event to the run's inbox) | `{"type": "permission_request", "id", "title", "kind", "options": [{"option_id", "name", "kind"}], "detail"?}` |
| Answer (renderer → host → the run) | `{"t": "permission_answer", "id", "option_id"}` or `{"t": "permission_answer", "id", "cancelled": true}` |
| Resolution (stream event to the inbox) | `{"type": "permission_resolved", "id", "option_id"}` or `{"type": "permission_resolved", "id", "cancelled": true, "timed_out"?: true}` |

A card that times out is followed by an `{"type": "error", "message"}` event.

## Public modules

Beside `gen`, a generator may import these `collimate` modules: `permissions` (the grant), `fs` (symlink-safe file access under a directory a container can write), `git_hosts` (the pure Git account checks: the published host keys of github.com and gitlab.com, known_hosts and OpenSSH private-key parsing, and the account address rules), `pointers` (the `.terminal` / `.website` / `.chat` pointer shapes, the host's `starting_pointer` placeholder and `is_starting_pointer`, and `is_run_path` / `run_label` / `run_card_path`, which read a path the way the host reads run folders), `surfaces` (`surface_path`, `surface_declaration` (the run's inbox registration), and `resolve_opens(manifest, fields, values, *, folder)`, the files a run opens, read the way the host reads them), `transcript` (the chat-transcript format), `config_values` (`parse_set_yaml`, sparse `resolve_generator_values` and dense `resolve_form_values`), `providers` (the model providers: one row each, with its API family, standard endpoint, key environment name, default model and plan), `account_taxonomy`, `harness_placement`, `harness_catalog` and `host_harness` (harness metadata: which harnesses a model account serves, where a harness runs under a grant and the least grant it needs, the per-harness facts generated from the Agent's harness adapters, and whether a harness CLI is on this machine), `options_stack` (options declaration parsing, path mapping and folder naming), `testing`, `shared` (this SDK's own files at runtime: its `shared/` tree, the toolchain contract, and `sdk_distribution`), and `handlers` (handler validation, path/type lookup and accepted skill validation). One more public tool namespace is `lint` (the `collimate-lint` checks, `declared_enforces`, `GeneratorRef.from_manifest(manifest, *, read_file=None)`, and the public `GENERATOR_FIELDS`, `RENDERER_FIELDS`, `RUNNER_FIELDS`, `GENERATOR_RUNS_ON`, `CONFIG_FOLDER_FIELDS`, `ACCOUNT_REQUIREMENT_FIELDS`, `ACCOUNT_PROPERTY_ROOTS` and `ACCOUNT_PROPERTY_FORBIDDEN` sets). Generator build/test tooling may import `sdk_distribution` from `collimate.shared` to read this SDK source's build-checked canonical metadata and complete runtime requirements; it never selects another ambient installation and requires filesystem-backed SDK packages. Every other module — `native` (the implementation behind `gen`; import `gen` instead), and the `_`-prefixed ones, the host's plumbing among them — is internal.

See [generator authoring](how-to-write-a-generator.md), [runner authoring](how-to-write-a-runner.md) and [testing](testing-generators.md).

Lint requires `main_config`. Omitting a runner target resolver permits partial visibility; supplying a resolver that returns `None` reports a missing target. `GeneratorRef.from_manifest` gathers target facts without a second cache. See the [generic surface design](generic-surface-design.md) for the ownership boundary.

A file mirror can be used as `async with gen.live_files() as mirror`. Await
`mirror.flush()` before closing a surface that can request output corrections:
it waits for the final push acknowledgement and raises read, transport, or
partial-refusal errors, including a previously failed background push. Await
`gen.commit()` for additionally staged SDK output. A run that ends on its own
does this while a correction turn can still run, before reporting success.

A refused mirror push stays in the workspace. The mirror neither retries that
path nor replaces it from the cell until its workspace bytes change again.
Pass a synchronous `on_refused(path, error)` callback to report each refused
attempt; it runs once for that attempt, and the refusal is also logged.
Successful siblings remain committed. A subsequent unchanged flush does not
resend a held file; edit it to release the hold and validate it again.


## Handlers and verification

The Agent generator exposes `list_handlers`, `handler_skills` and
`validate_file` on every tool surface, independently of file/web-tool
settings. They use these same SDK operations; external harnesses receive them
through the owned per-run bridge, including terminal sessions. A bridge
startup failure is reported in the run log.

`gen.handlers()` returns `list[HandlerInfo]`: each record has `id`, `name`,
`description` and `types: list[HandlerType]` (extension and optional `render_as`
alias). `gen.handler_for(path_or_ext)` resolves the
longest case-folded suffix or returns `None`; clashing claims serve neither
handler. `gen.handler_skills(ext_or_id)` returns accepted, namespaced skill paths
mapped to text. These capabilities are available without an extra permission.

`gen.validate(path, verifier=None)` returns `ValidationResult` with boolean
`valid` and an optional error string. An explicit verifier is a relative path
inside the run workspace. Validation sees current workspace package siblings;
a type without a verifier is valid. A missing compiler or invalid verifier
fails explicitly. Verifier execution uses a fresh watched QuickJS context and
a virtual native compiler callback, not renderer execution or a Node fallback.
See [Verifier authoring](how-to-write-a-verifier.md).

In tests, `stub_handlers(records, skills=...)` supplies handler metadata and
accepted skill maps; `stub_verifier(ext_or_explicit_path, fn)` receives exact
target bytes and returns a verdict. These fixtures do not prove actual native
compilation; the renderer handler integration suite does.


### Renderer preview primitives

These are members of the [version 1 renderer host contract](interface.md),
including their inputs and result shapes; changes are interface changes.

`RendererAPI.getFilePaths()` returns body-free membership of the renderer's own
cell (or its selected immutable History version). `api.preview.compile(files,
entrypoint)` compiles a `Record<string, Uint8Array>` without executing or approving
it and returns `{source_hash, code, bundle_hash, runtime: {react, session},
apiOrigin, imports, artifact_hash}`. Runtime strings are the host's served SDK modules, not capabilities.
The same members exist in native and iframe hosts. See the renderer authoring
guide's sealed-preview section for bounds, per-mount write permission and the
explicit preparation and frame self-navigation limitations.

### Preparing sealed preview dependencies

`api.preview.prepareDependencies(files, entrypoint, signal?)` is an explicit online action. The sealed preview offers **Prepare dependencies** when a pinned dependency is missing, and labels it as the only preview action that accesses the network. Preparation compiles and parses literal import/export edges outside the frame, downloads inert module bytes through the app's module mirror, executes nothing, and freezes exact bodies and SHA-256 hashes locally for that source identity. An optional AbortSignal cancels preparation when its owner unmounts. `api.preview.compile` never downloads dependencies: it rejects unprepared literal dependencies. Computed imports use a confined importer: targets in the frozen graph succeed; targets outside it refuse. Add a literal import edge when an otherwise computed dependency must be prepared. Runtime and common React/ReactDOM dependencies ship prepared and share one React identity.

The compilation result carries `imports`, the frozen offline import map, and `artifact_hash`, which includes source, runtime and prepared dependency bodies/hashes. Package approval continues to use the unchanged package content identity. The sealed frame has no CDN script permission, no connection permission and no dependency proxy. Literal imports outside its frozen graph fail closed. An embedded preview cannot prepare dependencies or expand its initial graph. Read/write approval and navigation revocation are unchanged.

Pinned module roots use HTTPS with an exact semantic version in the path (for example `https://esm.sh/demo@1.2.3`, `https://cdn.jsdelivr.net/npm/demo@1.2.3/dist/main.js`, or `https://unpkg.com/demo@1.2.3/module.js`), or a complete 40/64-character hexadecimal commit/content-hash path segment. Credentials, fragments, known moving tags and version ranges refuse. There is no finite dependency origin catalog. Same-origin child URLs such as esm.sh `/node/process.mjs` need no separate version token: their exact bytes and hashes belong to the frozen graph. Cross-origin child URLs require the same explicit root pin format. Bounds are 512 modules and 64 MiB per graph, and preparation has a 30-second deadline composed with cancellation.
