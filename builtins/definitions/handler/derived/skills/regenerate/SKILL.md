---
name: regenerate
description: Regenerate a built-in handler's skills in a work cell from its guidance and every example, for the user to offer back and review.
---

# Regenerate a built-in handler's skills

This cell is a work copy of one of the app's built-in handlers. The YAML
block at the top of brief.md names the handler (`handler:`) and its folder in
this cell (`package:`). The paths below are relative to the cell and start
with that folder.

A built-in handler is part of the app: its definition, guidance, verifier and
shipped examples are fixed. A channel may add examples to it and regenerate its
skills, nothing else.

Read the package's `.handler` definition and its guidance, and every example in
its `examples` list with its description, expected verdict (`expect`) and
feedback: the shipped examples and the ones this channel added, each in
`<package>/examples/<folder>/`. Start from the current skills in
`<package>/derived/skills/`. Rewrite them so they teach the guidance with
concrete examples drawn from those folders, the added ones included. The
handler authoring skill is in `guides/handler-authoring/`.

Write only `<package>/derived/skills/**`, and write nothing else in this cell.
Some files under `derived/` are fixed: `derived/verify.ts`, every skill folder
that holds an `assets/` folder and every `references/` folder, and, for the
`handler` handler, its `build`, `regenerate` and `create` skills. Leave those
exactly as they are; only the other skills are taken back. Never change the
definition, guidance, examples or `derived:` line.

Check every example's covered files against the unchanged verifier and compare
each verdict with its `expect`: with a `validate_file` tool, call
`validate_file(path, verifier="<package>/derived/verify.ts")`. The verifier
cannot change, so report every example whose expectation the verifier
disagrees with, with its feedback, instead of working around it.

Finish by leaving the skills in place and reporting the checks and any open
questions. The user offers them to the built-in handler from this cell's Files
panel with **Use these skills** on its `.handler` file; they then wait for
review on the built-in's card in Capabilities. Nothing is used until the user
reviews and accepts them. Do not report the skills as in use.
