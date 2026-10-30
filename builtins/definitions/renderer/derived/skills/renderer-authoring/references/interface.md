# The Collimate interface — version 1

This page records the shared contracts for files, handlers, generators and
renderers. Version 1 names the documented contract; it does not add a `version`
field to existing IPC messages or verifier inputs. Changes to these shapes or
the rules below require an explicit interface decision.

## Files, history and approval

Files have types supplied by accepted handlers. Cells retain authored history.
A run reads and writes only its own cell through the host. Invalid typed run
writes are refused; a person's invalid edit is retained and visibly flagged.
Generator, runner, renderer and handler packages are in use exactly while they
are in a cell of the channel's Capabilities stack; no run works on that stack.
A package reaches it through approval (Add to Capabilities), import, or a move
of its cell. A handler's build is in use only when its hash is accepted, by
Review and accept or as a build the app ships, wherever its bytes came from. The SDK and host contracts do not depend on a particular model or
agent approach.

## Handler packages and skills

A package contains its `.handler` definition, examples, and its build: the
checks in `derived/verify.ts` plus the skills in `derived/skills/<name>/`.
Review and accept records the exact hash of `derived/`, which puts that build in
use; a `derived` line naming any other hash is never trusted. A build
is written in a work stack (Build opens one) and returns through Add to
Capabilities with Replace, which keeps the target's `derived` line, so the
build in use stays in use until Review and accept. Review and accept checks the
examples against the build.

A user-authored handler can also contain `skills/<name>/` beside `derived/`.
These are additional text guidance, loaded alongside the build's skills in use
through the per-type catalog and ordinary approval into Capabilities. They are shown
separately in the catalog and viewer. They are excluded from the derived hash
and Build inputs; they cannot alter the handler's verifier.

The `.handler` verifier refuses skill-name collisions with the handler's
skills in use, nested `derived/`, `verify.ts`, scripts and non-text content. Additions are bounded to 128 files and 1 MiB per handler.
See [Handler authoring](how-to-write-a-handler.md) for exact names, allowed text
and layout rules.

The reserved types (the definition types, `.options_stack` and `.git-checkout.json`) belong to the app's built-in handlers. Their definition,
guidance, verifier and shipped examples are part of the app and never change in
a channel. A channel may add examples to a built-in handler and regenerate its
skills in a work stack; the regenerated skills return through Use these skills,
wait for review and take effect only after Review and accept, are checked
against the unchanged verifier, and stay in the channel.

## Verifier input and verdict, version 1

The synchronous verifier receives this shape:

```ts
type FileMetadata = { size: number; hash: string };
interface VerifyInput {
  path: string;
  text?: string;
  bytes?: FileMetadata;
  previous_text?: string;
  previous_bytes?: FileMetadata;
  package: Record<string, string | FileMetadata>;
  cell_paths: string[];
}
type Verdict = { valid: true } | { valid: false; error: string };
```

The target and package describe the proposed post-write snapshot. Previous
content is this target's committed pre-flush content, never a pending draft;
both previous fields are absent for a new file. Text and opaque metadata are
alternative representations. `cell_paths` gives membership, not other-cell
access. Promises and malformed verdicts are refused. Verifiers have no ambient
filesystem, network, process or environment authority. See
[Verifier authoring](how-to-write-a-verifier.md) for bounds and compiler helpers.

## Served files and the run's inbox

A run's pointers (`.chat`, `.terminal`, `.webview`, `.website`) live in its run
folder and have the types the manifest declares under `ui_files`. The manifest's
`opens` names the files a run serves as its surfaces: the host writes a starting
placeholder for each pointer path, claims the paths as starting surfaces, and
reveals the first before the process spawns. A claim turns live on the run's
real pointer write or its inbox registration. Release drops the claim at run
end; run folders are swept then, with their content retained in Logs. Cell
files persist.

Each run registers one inbox: the `.chat` surface `gen.serve_chat` serves, or a
transport-only session (`files=False`) that writes no file, optionally
broadcast so several viewers attach. User messages, interrupts, approval
answers and config changes arrive through it; approval cards and stream events
leave through it. Renderers read a pointer's live session with
`api.getSurface()` and `api.onSurfaceChange`. Session coordinates live in the
host's registry, never in a binding file.

## Annotations, version 1

An annotation context is a message store: one per run (`context_id` is the run
id) and one per cell (`"cell"`), whose messages wait until the user sends them
to a run. A message has an `id` (`a_` and 16 lowercase hex digits), a markdown
`body` (at most 64 KiB), an `author` (`user` or `run`, with the run's
`author_label`), a `kind` (`message`, or `prompt` for the run's seeded prompt),
a `parent_id` naming a top-level message (replies are one level deep), a
`mode` (`queued` or `interrupt`), `context_items` (`{path, renderer_id, label,
text, anchor}`, the renderer's answer to the user's selection, with `path` and
`renderer_id` stamped by the host), `refs` (cell paths the body links), and
`created_at`, `edited_at` and `read_at` in UTC `YYYY-MM-DD HH:MM:SS`.

A pull marks every unread message the run can see read in one step, interrupt
mode first, and reports the context's revision, unread count and interrupt
counter as it left them, so an interrupt the batch answers is never taken for a
newer one. Read messages are locked; messages held below the queue bar are
invisible to the run. A run's posts and the seeded prompt are born read. At run
end the host seals the run's context and writes it to the run's Logs as
`annotations.json`. Generators see messages as `gen.Annotation` through
`gen.annotations` (see the [API reference](api-reference.md#annotations)).
Renderers answer a host's request for context with
`api.onAnnotationRequest(handler)`: the handler receives `{id, mode: 'pick' |
'selection', hint?}` and an AbortSignal and resolves `{label, text, anchor?}`
or null; the host stamps `path` and `renderer_id` (see the
[renderer guide](how-to-write-a-renderer.md)).

## Renderer host members, version 1

`api.getFilePaths()` returns `Promise<string[]>` containing body-free membership
of this renderer's own cell or selected immutable History version.
`api.preview.compile(files, entrypoint)` accepts exact `Uint8Array` file bytes
and returns `{source_hash, code, bundle_hash, runtime: {react, session}, apiOrigin, imports, artifact_hash}`.
Compilation neither runs nor approves the renderer. Both native and iframe
hosts expose these members. Limits are 5,000 files, 64 MiB, four concurrent
compiles per host page, 30 seconds for the compiler, and 2 MiB per returned
runtime module. Runtime strings and API origin grant no authority. See the
[API reference](api-reference.md) and [renderer guide](how-to-write-a-renderer.md)
for the remaining host members and preview rules.

Unapproved previews disclose the explicit dependency preparation action. Use an example file when
previewing untrusted code. Per-mount edit approval, source checks and the opaque
iframe protect the host; frame self-navigation remains an outbound channel. A preview is not approval into Capabilities.

### Preparing sealed preview dependencies

`api.preview.prepareDependencies(files, entrypoint, signal?)` is an explicit online action. The sealed preview offers **Prepare dependencies** when a pinned dependency is missing, and labels it as the only preview action that accesses the network. Preparation compiles and parses literal import/export edges outside the frame, downloads inert module bytes through the app's module mirror, executes nothing, and freezes exact bodies and SHA-256 hashes locally for that source identity. An optional AbortSignal cancels preparation when its owner unmounts. `api.preview.compile` never downloads dependencies: it rejects unprepared literal dependencies. Computed imports use a confined importer: targets in the frozen graph succeed; targets outside it refuse. Add a literal import edge when an otherwise computed dependency must be prepared. Runtime and common React/ReactDOM dependencies ship prepared and share one React identity.

The compilation result carries `imports`, the frozen offline import map, and `artifact_hash`, which includes source, runtime and prepared dependency bodies/hashes. Package approval continues to use the unchanged package content identity. The sealed frame has no CDN script permission, no connection permission and no dependency proxy. Literal imports outside its frozen graph fail closed. An embedded preview cannot prepare dependencies or expand its initial graph. Read/write approval and navigation revocation are unchanged.

Pinned module roots use HTTPS with an exact semantic version in the path (for example `https://esm.sh/demo@1.2.3`, `https://cdn.jsdelivr.net/npm/demo@1.2.3/dist/main.js`, or `https://unpkg.com/demo@1.2.3/module.js`), or a complete 40/64-character hexadecimal commit/content-hash path segment. Credentials, fragments, known moving tags and version ranges refuse. There is no finite dependency origin catalog. Same-origin child URLs such as esm.sh `/node/process.mjs` need no separate version token: their exact bytes and hashes belong to the frozen graph. Cross-origin child URLs require the same explicit root pin format. Bounds are 512 modules and 64 MiB per graph, and preparation has a 30-second deadline composed with cancellation.
