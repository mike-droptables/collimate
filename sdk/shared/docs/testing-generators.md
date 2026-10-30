# Testing generators

Use `collimate.testing.run_generator(entrypoint, inputs=..., config=..., prompt=..., accounts=...)` to execute a generator host-free. These are test fixtures, not runtime invocation overrides. Account fixtures map role names to complete snapshots. Do not put real credentials in tests.

`selected_file="notes/draft.md"` is the cell-relative path the run reads as `gen.selected_file`, written where the host writes it (`.collrun/selected_file`); seed the file itself through `inputs`. Leave it out for a run with no selection (`gen.selected_file` is `None`).

Use `run_generator_callable` for an already-imported coroutine. The recording bridge captures the run's output files, logs and lifecycle calls. `permissions` supplies its grant; use synthetic accounts and inputs.

Read a result by the paths your generator declared: `result.output_text("answer.md")` returns text or `""` for an absent/binary file; `result.structured("answer.json")` returns parsed JSON or `None` for absent, empty or invalid JSON. `result.structured("answer.json", MyModel)` validates an object with `MyModel(**data)`: a non-object JSON value raises `ValueError`, and invalid fields raise the model's validation error. These helpers have no fixed Agent output path.

`gen.commit_account` replaces only fields the account fixture already stores (its `secret_fields` and `secrets`, plus `expires_at` and `token_type` unless its `kind` is `custom`), each with a nonempty string, and merges them into the fixture; a `None` (a removal), an empty or non-string value, or any other field raises `RuntimeError` and changes nothing, as on the host.

`approvals` scripts the human behind `gen.ask`: `None`, the default, means nobody can answer — `gen.can_ask()` is False and every card is refused at once; a list answers the cards in order and a callable `fn(card)` answers each, where an answer is the chosen `option_id`, `None` to dismiss the card, or `collimate.testing.UNANSWERED` to leave it waiting until its timeout. As in a real run, a card is shown only while the generator's `gen.serve_chat` serves its chat session (pass `messages=[]` to serve a chat that ends once its turns settle) and the session can show it: its `.chat` surface, or a broadcast inbox (`files=False, broadcast=True`). The emitted cards are in the result's `tier_b_calls` (op `stream_event`) and in the bridge's `stream_events`.

The run's identity is a fixture too: `runner={"runner": <id>, "name": <display name>}`, `run_id`, `label`, `channel` and `cell_id` shape what `gen.run_info()` returns (by default a fresh run id and the label `dev-<run_id[:8]>`, so the run folder is `_runs/dev-<run_id[:8]>`). `foreign_runs={label: {path: content}}` seeds sibling runs of the same cell, their paths relative to `_runs/<label>/`: the generator reads their files, and its writes into their folders raise `PermissionError` as they would under a host. `gen.reveal_file` calls are in `tier_b_calls` (op `reveal_file`, with `committed` saying whether the host would have shown the file). `ui_files=[".chat", ".json"]` is the manifest's UI file list the host would pass: `gen.update_ui_file` and own-run surface helpers then refuse any other suffix, as a hosted run does; the default `None` accepts every suffix.

`annotations=[...]` scripts the user's messages in the run's annotation context: a str is a queued top-level message, and a dict takes `text`, `mode` (`"queued"` or `"interrupt"`), `reply_to`, `context` (`gen.ContextItem` values or dicts) and `refs`. A non-empty `prompt` is seeded first as a read `kind="prompt"` message. `bridge.add_annotation(text, mode=..., reply_to=..., context=..., refs=...)` adds one mid-run and returns its id; an interrupt-mode message advances `interrupt_seq`. The run's posts are in `bridge.annotation_posts`, pulls and posts in `tier_b_calls` (ops `annotations_pull` and `annotations_post`), and `result.annotations` holds the whole context, with `read_at` set once pulled. Scripted annotations hold a `gen.serve_chat` run open until they are pulled, and a run that polls its annotations stops on the second idle poll, so script them only for a generator that reads them.

Test the behavior a user depends on: complete configuration loading, required supplemental files, account capability matching and cancellation. No test should require the user's keyring, account, container or network unless explicitly marked as a live integration test.

Run `collimate-lint generators --strict` to validate generator-owned configs and saved runners together. Tests that read a saved runner back must find its text and binary supplemental files, prompts, search tags and local account references, and no secrets.

## Compile-check a renderer

The accepted renderer handler validates a `.renderer` definition, bundles its package with the pinned native compiler and statically checks a `defineRenderer(...)` result with a `component`. It never executes React: mount the renderer to test hook usage, effects and layout. Runtime verification works on an installed SDK without Node. See [Verifier authoring](how-to-write-a-verifier.md).

In an SDK checkout, run `npm --prefix scripts ci` before the pytest suite (it puts esbuild and tsx under `scripts/node_modules`); without it every node-backed test (renderer model tests and the webview's node test) skips and the run still looks green, so read the skip summary (`-rs`).

For an options workflow, `run_generator(..., options=...)` seeds the selected
`.options_stack`; supply candidate files through the regular file fixture.
Use the declaration shape `{options: [{name, root, files}], rest}` and retain
binary bytes in the candidate files. Assert the declared roots and globs map to
real files through `collimate.options_stack`, rather than inventing another
layout reader.

## Test a draft from an agent command

The agent's `test_generator(path, settings, prompt)` tool executes a packaged
`.generator` in the same place as that agent's commands: its command container,
or the host for a host CLI. It is absent under `commands: deny` and asks for a
command card under `ask`. Draft code never executes in the parent generator's
Python process. A test shares the parent's cell, workspace, identity and grant;
its writes go through that parent's normal publication checks.

Each test times out after two minutes and returns the last 64 KiB of combined
stdout/stderr, explicitly marked when truncated. It can change the shared cell
immediately; a successful test is not a dry run or an atomic transaction.

`collimate.testing.forward.Forwarder` provides the token-authenticated listener.
`prepare_child(relay, path, settings=..., prompt=..., selected_file=...,
child_workspace=..., ipc_host=..., sdk_path=...)` stages configuration (and
the child's `gen.selected_file`) and returns
`PreparedChild(argv, cwd, env, run_dir, host_run_dir)`. The caller executes that
`uv run` command in its existing command environment, with the entrypoint's
declared inline dependencies and selected optional dependency group. The
settings overlay the draft's main config. `COLLIMATE_RUN_DIR` holds the private
prompt/config separately from the parent, and the public file API hides it.
Call `child.close()` after execution. The command image contains `uv` and the
current SDK source; its content hash includes those SDK bytes.

The relay forwards only `update_cell`, `read_cell_files`, `reveal_file`,
`client_caps`, `list_handlers`, `handler_skills` and `validate_file`. The
annotation ops are not relayed: a child's `gen.annotations.available` is False.
Request ids are remapped under the real parent's request lock. `finish`, `phase`,
`progress` and `port_bound` are recorded locally in `relay.result`.
Surface/session/terminal, environment and account operations are refused. A
restricted child declares `COLLIMATE_IPC_OPS`; its live mirror computes hashes
from the permitted file reads instead of asking for an extra operation. This
restricted transport reads full selected bodies, so it does not provide the
normal host's body-free hash optimization.

After the process exits, `relay.wait_finished(timeout)` allows its buffered
finish frame to arrive. It returns true for either a successful or failed finish;
inspect the result and process exit code. Closing the relay wakes waiters and
stops future requests. Cancellation cannot roll back an update already issued
to the parent. A draft requiring a surface, terminal, environment or account must
be added and tested through a real runner instead.
