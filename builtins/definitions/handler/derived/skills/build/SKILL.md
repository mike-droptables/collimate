---
name: build
description: Build a handler's checks and skills in a work cell, for the user to add back to Capabilities and review.
---

# Build a handler's checks and skills

This cell is a work copy of one handler package. The YAML block at the top of
brief.md names the handler (`handler:`) and its folder in this cell
(`package:`). The paths below are relative to the cell and start with that
folder: when `package:` is `note/`, the verifier is `note/derived/verify.ts`.

Read the package's `.handler` definition, its guidance, and every example in
its `examples` list with its description, expected verdict (`expect`) and
feedback. Each example's files are in `<package>/examples/<folder>/`. Read the
verifier guide and the interface page in `guides/build/references/`, and the
handler authoring skill in `guides/handler-authoring/`.

Start from the package's existing `derived/` files when present. Write only
`<package>/derived/verify.ts` and `<package>/derived/skills/**`. Never change
the definition, its guidance, its examples or its `derived:` line, and write
nothing else in this cell. The package's added skills (`skills/`) stay with the
handler in Capabilities; do not write a `skills/` folder here.

Write a synchronous TypeScript verifier importing only `collimate/verify`, and
skills that teach the guidance with concrete examples. A verifier receives only
its target file, the package's other files and the cell's paths; it has no
filesystem or network. Check every example's covered files against the draft
verifier and compare each verdict with its `expect`: with a `validate_file`
tool, call `validate_file(path, verifier="<package>/derived/verify.ts")`.
Correct the verifier and skills until every expectation holds.

Finish by leaving `<package>/` in place and reporting the checks and any open
questions. The user adds the package back from this cell's Files panel with
**Add to Capabilities** (Replace), then reviews the build on the handler's card
in Capabilities. Nothing this build makes is used until the user reviews and
accepts it; the build in use stays in use until then. Do not report the build
as in use.
