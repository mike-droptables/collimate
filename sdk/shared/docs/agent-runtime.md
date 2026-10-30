# Agent generator runtime

The Agent cell ships one SDK-owned generator, `agent`: a thin entrypoint in
`generators/agent/agent/` over the session runtime in
`generators/_agent_shared/session/`, which reads the run's surface
(`annotation`, `chat` or `terminal`) from its `surface` setting. Its models,
harnesses, prompts, native credentials and tool policy live in
`generators/_agent_shared/` and in the generator's settings and presets. The
surface file a chat or terminal run serves is the one its manifest's `opens`
resolves to (`collimate.surfaces.resolve_opens`), the same file the host
created as a starting placeholder when it accepted the run; an annotation run
opens none. The SDK core supplies
ordinary run, file, account, environment and surface contracts. The app
enforces those contracts without knowing which model or harness a generator
uses.

## Harness ownership

Every harness is one folder, `generators/_agent_shared/harnesses/<id>/`: an
`adapter.py` declaring a `HarnessAdapter` (identity, locus, ACP argv, auth and
the providers it bills under its own provider ids, model rules, the
instruction seam, tools, the commands mechanism and the levels it can hold,
session paths, terminal, named hooks and its preparation function), a
`backend.py` holding that preparation and the harness's real quirks, and an
`image/` build context for a container harness. `harnesses.HARNESS` is the
roster; a missing facet is an error, and the completeness test checks every
row. Every harness speaks ACP: pydantic-ai is Pydantic AI's own ACP adapter
serving its `Coder` agent, a process on this machine in an environment of its
own (`host_adapters.ensure_python_adapter`, pinned exact), whose shell runs
in a Docker sandbox the host places under `docker: full` and on this machine
otherwise; claude-code is
claude-agent-acp on this machine; the others run their ACP adapter in their
container.

A leg goes through one pipeline (`harnesses/pipeline.py`): it resolves the
authentication and provider (refusing one the harness cannot bill), the
commands level (a level the harness's mechanism cannot hold resolves down,
with a notice), and the credentials (the selected key, or the plan's leased
login), writes a `home_file` instruction seam, then calls the harness's own
preparation and applies its declared facets to the spawn profile. Every
provider fact — wire, standard endpoint, key environment name, default model,
plan — is one row in `collimate.providers`; each harness bills its provider at
that registry endpoint. Where a run may place a harness, under which grant, is
one function, `collimate.harness_placement.placement` (with `adopt`, the
Annotation surface's grant adoption, for a generator whose manifest declares
`adopts_harness_grant: true`), used by the config parser, the linter,
the host's run start and the Key manager. The core catalog
(`collimate.harness_catalog`), the settings schema's harness options and the
presets are generated from the adapters by `scripts/sync_harness_catalog.py`,
which a test pins byte for byte.

`harness_homes` owns run workspace homes and session-store handling.
`host_adapters` owns supported host discovery and adapter installation, with
versions in one table. `acp_client` owns transport and protocol behaviour,
including the capabilities it probes at `initialize` (load, resume, list,
fork, close, MCP transports, prompt content) and the per-call record that
fills what a later update or permission request leaves out; `acp_policy` owns
the shared protocol permission predicates; `acp_run` coordinates sessions.
Workspace and web tools live in `workspace_tools` and `web_tools` and reach
every harness over the MCP bridge. `agent_config` owns runner coercion, model
selection and grant validation; `agent_events` owns turn results, bounded
event/attachment helpers and prompt notes. The shared package remains the
turn and run-lifecycle composition root and exports its public generator
entrypoints. Internal helpers import their implementation owner directly.
Account taxonomy is shared SDK metadata and does not import this generator
runtime.

Each harness is checked by the scripted-provider conformance suite
(`tests_live/test_live_harness_conformance.py`): its `initialize` against the
vendored ACP protocol matrix, a turn through the bridge, the session record
and its reopen, and the commands grant at every level.

## Grants and tools

The run's grant is checked before native permission prompts. `commands: deny`
does not expose the generator-testing command tool. `commands: ask` uses the
existing approval flow; `allow` permits the selected harness's command surface.
Ordinary file-handler tools remain available independently of command access.
Native session-mode changes and loaded-session permissions must continue to
honor the run's grant. A saved approval cannot silently widen the next leg.

The native Python backend and MCP delivery share the workspace tool registry.
File tools retain path containment and typed-write verification. Web tools
retain address checks, DNS pinning and redirect validation. Model selection,
provider SDKs and interpretation of native permission messages stay here in the
generator rather than moving into SDK core or the app.

Pi reserves these tool names for its Collimate bridge extension:

- `collimate_list_handlers`
- `collimate_handler_skills`
- `collimate_validate_file`
- `collimate_list_files`
- `collimate_read_file`
- `collimate_grep`
- `collimate_view_image`
- `collimate_write_file`
- `collimate_edit_file`
- `collimate_annotate`
- `collimate_list_annotations`
- `collimate_web_fetch`
- `collimate_web_search`
- `collimate_list_skills`
- `collimate_load_skill`
- `collimate_test_generator`

This is the complete union across run configurations. Handler tools are always
available; file, web and skill tools follow the enabled file tools, web search
and available skills, and the annotation tools follow the Annotation surface.
The generator-testing tool requires `commands: ask` or
`commands: allow` and a command context. Pi refuses a collision with another
extension using one of the registered names.

## Instruction context

ACP session creation has no portable system-prompt field. A backend uses its
supported native instruction file or adapter metadata and records whether that
context landed. The file is in its managed home, refreshed for the current run
and cleared when empty. Where no native mechanism exists, a bounded, marked
prefix carries context into the first turn; transcript normalization removes
that prefix from the user's conversation. Native configuration and instruction
files are not written into the user's unrelated home.

Accepted handler skills are read through the handler tools (`list_handlers`,
`handler_skills`, `validate_file`); the handler owns a type's syntax and
verification.

## The annotation surface

An interactive annotation run subscribes to its annotation context
(`gen.annotations`) and pulls only when it is idle, inside the turn that
presents the batch, so a queued correction never holds a stale batch. A batch
lists interrupt-mode messages first, then the messages a cut turn left open,
then the queued ones. Each message renders with its id, thread, author and
mode, the context items the user picked in a file, and the cell files its body
links: text inlined up to 32 KiB a file and 128 KiB a batch, at most 8 media
files attached, anything else named for the file tools.

A newer interrupt cancels the running turn (`gen.session.interrupt_turn`), and that
turn's messages come back first in the next batch with a note that it was cut
short. A stopped or failed turn keeps its messages open the same way; a failure
also posts a note in the thread. A completed turn that posted nothing through
the `annotate` tool has its final answer posted under the batch's last thread.
Files edited outside the run's turns become a note in the next batch, never a
turn of their own. A host refusal of a write produces at most two consecutive
correction turns per refused path, on the annotation and chat surfaces. After
15 idle minutes the session's ACP adapter, container and bridge are parked; the
next batch reopens them.

A one-shot never subscribes, so its context reports it as not listening. Its
single batch is the host-seeded prompt message plus whatever one pull finds at
its opening turn, its reply goes under the prompt message, and it ends once its
corrections settle and its final push is acknowledged.

## Credentials and container execution

Only the selected account supplies credentials. Supported native private files
replace provider-key environment variables; host-derived account bindings remain
host-authorized. Native files use restrictive permissions and safe writes.
Some supported harnesses require a process environment credential. These are
documented per backend and are never silently presented as private-file routes.

Cline's chat leg, Antigravity's ACP server, Hermes and Mistral Vibe have no
provider-key file route: the selected provider key stays in the harness
process environment, and with `commands: allow` permitted commands can read
it. GitHub Copilot CLI keeps its token or key in its environment but strips it
from the commands it runs. pydantic-ai reads its key from a private file it
removes at start; its commands get only the workspace's own environment (on
this machine), or run in its sandbox container under `docker: full`, which
the key never reaches.
The command grant UI states each harness's reach.

`commands: allow` gives native commands access to the harness's permitted files
and process environment. A credential file is not a secret vault against those
commands. File routes prevent ordinary environment dumps and inherited child
environments from exposing keys; they do not promise that an authorized native
command cannot read its own credential. Known selected secrets are scrubbed at
log, adapter, session and transcript persistence boundaries. An unrecognized
credential or arbitrary sensitive user content is not covered by that claim.
Native histories containing a selected secret are omitted from durable capture
where safe rewriting cannot preserve the native format; that may cost resume
history for that session.

Completed commands and whole-file reads use the host-mediated environment
plane. The environment returns bounded command output, so exports whose full
contents matter use a private temporary file and the chunked file reader.
Attached stdio retains its direct stream transport. Teardown signals adapter
processes, closes clients and releases environments without blocking the main
event loop for container shutdown.

## The CLI surface

A harness's `Terminal` facet declares where its TTY runs, independent of where
its turns run: `container` (the harness's container, `cli_container`), `host`
(the unmodified vendor CLI on this machine, `cli_host`), or a
`"module:function"` target resolved per request from the account's auth
(`harnesses.terminal_locus`). Claude Code's is `host`; Antigravity's is `host`
on the Google plan and `container` on a Gemini key; every other harness's is
`container`. The catalog projects the resolved locus per auth
(`terminal_locus`), and `harness_placement.placement` places the CLI surface
by it: a container CLI needs `docker: full`; a CLI on this machine needs
`docker: none` or `full`, `commands` other than `deny`, and the CLI installed
here, and runs outside any sandbox. Only the Annotation surface ever adopts a
raised grant.

A leg on this machine runs the CLI placement found, in the cell's real
workspace path, with an environment of its own that replaces the child's
(PATH led by the host adapters' node, HOME, TERM, the locale and what the leg
adds) and a per-account home, `<cache>/collimate/host-homes/<harness>/<account>/`
(owner-only, keyed by the Key manager entry's credential account), where the
CLI keeps the sign-in its own flow writes. A plan leases such a leg nothing:
Antigravity's agy gets no credential, and Claude Code's gets the entry's setup
token only when it carries one. A plan whose CLI signs in on this machine is
therefore complete in the Key manager without its paste, and only the
surfaces that read the pasted credential refuse without it, naming it. The
per-cell parts stay in the cell's harness home: Claude Code's
`projects/<workspace>` folder in the per-account home links there for the leg,
so the tail, the export and the cell's session store read one record.

The leg serves its TTY with `gen.serve_terminal(None, argv, …)`, the host form,
which the host checks against the run's grant and placement record before it
registers the session. The host forks the argv on its PTY as its own session
with the PTY as its controlling terminal, so a resize is `TIOCSWINSZ` alone;
it labels the child with the run (`COLLIMATE_HOST_TERMINAL`), kills the
child's process group when the run ends, and at start kills what a killed
previous server of the same instance left.

## Terminal records

Terminal readers and final exporters share public parsing and session-location
helpers. Reader availability and construction use the same harness row. A reader
follows this leg's session, never an arbitrary earlier conversation. Transcript
normalization preserves complete tool-result payloads; live cards may use their
separate presentation bounds. Stat checks, offsets and settled-prefix merging
avoid repeatedly publishing the same content while a native record grows.

A transport failure is reported. ACP may make one bounded recovery attempt
when the native adapter supports reopening the session; it does not park a dead
adapter as a live one. Native terminals retain their documented session tier;
a test's ability to observe native output does not itself establish resumability.
