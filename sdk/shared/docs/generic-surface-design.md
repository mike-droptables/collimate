# Generic live surfaces

The SDK coordinates files, live sessions and output commits. Accepted handlers
own type-specific authoring skills and verification. Generators choose the
files they serve, keep one inbox per run, and talk with the user through the
run's annotation context.

## Files a run serves

A run's pointers (`.chat`, `.terminal`, `.webview`, `.website`) live in its run
folder, `_runs/<label>/`, and their types must be declared in the manifest's
`ui_files`; `gen.require_ui_types(context, *paths)` checks them before a
multi-file initialization. A generator also declares the files a run serves in
its manifest's `opens` (`{run}` and `{<main-config key>}` path templates under
optional conditions, read by `collimate.surfaces.resolve_opens`). When it
accepts a run, the host writes a `{"starting": true, "label"}` placeholder for
each pointer path in the run's first version, claims every path as a starting
surface, and reveals the first. A cell-scoped path another live run serves
refuses the start with `surface_taken`. The run's real pointer write (or its
inbox registration on that path) upgrades the claim to live; the run's end
releases it, and the host sweeps the run folder.

Renderers call `api.getSurface()` for their mounted physical path. It returns
the live session or null when the path is unserved or shown from History; its
`state` is `"starting"` (empty coordinates) while the host holds the path for a
run that has not served it yet, then `"live"`. Subscribe with
`api.onSurfaceChange(callback)` and fetch again after a `surface_changed`
event. Session coordinates never belong in a file's binding.

## The run's inbox

`gen.serve_chat` registers the run's inbox, one per run, at `pointer_file` with
its transcripts under `threads_dir`, both inside the run folder.
`files=True` serves the single-attach `.chat` surface. `files=False` is
transport-only: no served file, UI suffix requirement, claim or visible
run-file entry. `broadcast=True` (transport-only only) lets several viewers
attach, so approval cards reach every one of them.

User messages, interrupts, approval answers and config changes arrive through
the inbox; approval cards (`gen.ask`) and stream events leave through it. The
Agent generator's session machinery, `gen.session` (not the authoring
contract), queues the run's own turns (`gen.session.submit_turn(text,
source=...)`), cuts the running turn short (`gen.session.interrupt_turn()`),
pushes stream events (`gen.session.emit_stream_event`) and ends a session
gracefully (`gen.session.request_finish()`).

## Annotations

The run's annotation context carries the user's messages to the run and its
replies back. `gen.annotations.subscribe(on_new, on_interrupt=...)` watches it
(one poller per run, plain synchronous callbacks), `await
gen.annotations.pull()` takes every waiting message (interrupt-mode first) and
marks it read, and `await gen.annotations.post(text, reply_to=...)` answers.
The run is listening from its first poll. Pulls and posts are journaled and
replayed under the same operation id, so none is lost or doubled. A run reads
and posts only its own context; a `test_generator` draft child has none.

## Files changed outside the run

`live_files(skip_globs=(), on_changed=None, on_refused=None)` takes
generator-owned exclusions using case-sensitive `fnmatchcase`.
`on_changed(path)` fires after an external change is pulled locally; local
pushes and their echoes do not fire it. Refused local bytes remain in place and
`on_refused(path, error)` lets the caller request a bounded repair. The agent
allows at most two consecutive corrections and folds outside edits into its
next turn as a note.

## Renderer actions

Action consumers declare their surface path as `source`. Empty action filters
match everything.

## Output

Flushes drain staged writes and deletions, restoring pending work on a failed
commit without replacing newer edits. `GeneratorResult.output_text(path)` and
`structured(path, model=None)` read explicit result paths and have no
generator-specific output filename; `GeneratorResult.annotations` holds the
run's annotation context at its end.
