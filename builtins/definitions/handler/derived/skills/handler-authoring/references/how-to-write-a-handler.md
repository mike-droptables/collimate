# Writing a handler

A handler describes file types, their guidance and examples. Write a package
with one named `.handler` definition and an `examples/` folder. The definition
contains `id`, `name`, `description`, `types`, `guidance` and `examples`.
Each example names a folder below `examples/`, a description, an `expect` of
`valid` or `invalid`, and feedback explaining the expectation. Every listed
folder must contain a file of a covered type.

```yaml example=handler-example
id: note
name: Note
description: A note with a heading.
types:
  - ext: .note
guidance: Start with a heading, then explain the subject.
examples:
  - folder: basic
    description: One heading and a paragraph.
    expect: valid
    feedback: The heading states the subject.
```

Put the sample at `examples/basic/example.note`. Extensions match the longest
case-folded suffix. `render_as` chooses an existing renderer extension while
preserving the real saved filename. The definition types,
`.renderer_example`, `.options_stack` and `.git-checkout.json` belong to the app's built-in handlers:
Add to Capabilities and import hold a user handler claiming one, and the catalog leaves it out with
a problem. Clashing type claims among user handlers disable that type for all
claimants.

Author the package without `derived/` or a `derived` hash. Write its examples
first and the definition last, then call `validate`. The user adds the package
to Capabilities from the files panel (**Add to Capabilities…**). Its card in
the Capabilities stack's Manage view then offers **Build…**, which opens a work
stack beside Capabilities: one cell holding a copy of the package (without its
added `skills/`), a `brief.md` made from the built-in `handler` handler's build
skill, and the authoring guides. Run any runner there and have it follow
`brief.md`. A build is the handler's checks (the verifier, `derived/verify.ts`)
and its skills (`derived/skills/**`); the brief tells the run to write only
those. See [Writing a verifier](how-to-write-a-verifier.md).

The build must check every example against the draft verifier (with the
`validate_file` tool, `validate_file(path, verifier="<package>/derived/verify.ts")`)
and compare the actual verdict with its expectation. When it is done, add the
package back from the work stack's files panel with **Add to Capabilities…**
and Replace on, onto the cell it came from. A package may carry `derived/`
into Capabilities, but carrying it never puts it in use: the added build is
waiting for review, Replace keeps the target's `derived` hash and added
`skills/`, and only Review and accept on the card changes the build in use.
Validate runs every example through the waiting build's checks and changes
nothing, and Review and accept shows its checks and example results, then puts
that exact build in use by recording the hash of the `derived/` folder. Until
then the build in use stays in use; with none, files of these types are saved
unchecked and agents get no skills for them. Editing guidance does not silently
authorize new executable output: build again and accept to use it. Handler
skills teach the recipe for the source package; the separate build skill
teaches the build's output.

## Built-in handlers

The handlers for the definition files, options and the Git checkout record are part of the app: they sit on the Definitions card in every channel's
Capabilities stack, and their definition, guidance, verifier and shipped examples
cannot be edited. A channel can Add example to a built-in handler (each with
its expected verdict and feedback) and regenerate its skills. **Regenerate
skills…** opens a work stack whose cell holds the handler with this channel's
added examples and its current skills, and a `brief.md` made from the built-in
`handler` handler's regenerate skill. Run any runner there: the brief tells it
to write only the skills, check every example against the app's unchanged
checks and report the examples whose expectation the checks disagree with.
Then **Use these skills** on the handler's `.handler` file in that stack's
files panel offers the regenerated skills to the built-in. They wait for
review: agents keep the skills in use (the app's, or this channel's earlier
ones) until you Review and accept them, and Use the app's skills drops this
channel's regenerated skills, waiting or accepted. Added examples and
regenerated skills belong to the channel; exports and channel copies leave
them out.

## Adding guidance without rebuilding

Any approved handler package may also contain `skills/<name>/SKILL.md` and text
references beside `derived/`. These user-added skills supplement the skills
in use for every type it covers. They pass through ordinary package
approval and are available only while the handler is in Capabilities. The catalog
and viewer list the build's skills and added skills separately.

Added skills do not change the build's `derived` hash and are excluded from all
Build model inputs: a Build's work cell leaves them out, and adding its result
back with Replace keeps the target's. Review and accept preserves their bytes.

Use a unique folder name of 1–64 ASCII letters, digits, dots, underscores or
hyphens, starting with a letter or digit. A folder name or frontmatter `name`
cannot collide with another added skill, the skills of this handler's latest
build, or the skills in use during a rebuild. Frontmatter accepts LF or CRLF line endings, and effective names are trimmed
like runtime skill names before comparison without letter case. Other handlers have separate `<handler>--<skill>` namespaces.

Added guidance must be UTF-8 text: at most 128 files and 1 MiB in total per
handler. Markdown examples and text data such as JSON are allowed. Reject
binary data, NUL bytes, executable shebangs, any `scripts/` or `derived/` path
segment and `verify.ts`. Script and executable suffixes
are refused: `.py`, `.pyw`, `.js`, `.mjs`, `.cjs`, `.jsx`, `.ts`, `.tsx`, `.sh`,
`.bash`, `.zsh`, `.fish`, `.ps1`, `.bat`, `.cmd`, `.pl`, `.rb`, `.php`, `.lua`,
`.exe`, `.dll`, `.so`, and `.wasm`. Fenced examples inside Markdown remain text.
The `.handler` verifier enforces these restrictions; added skills cannot supply
new verifier logic.

## Verifier contract, version 1

The [interface page](interface.md) records the version 1 input and verdict shapes.
The current target is `path` with `text` or opaque `bytes: {size, hash}`;
`package` holds package-relative siblings and `cell_paths` post-flush membership.
Optional `previous_text` or `previous_bytes` is this file's committed pre-flush
content, absent for a new file, never a pending draft. A synchronous verifier
returns `{valid: true}` or `{valid: false, error: string}`. There is no runtime
version field. Changes to either shape require an interface decision. See the
[verifier guide](how-to-write-a-verifier.md) for isolation and resource limits.
