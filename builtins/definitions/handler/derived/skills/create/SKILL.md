---
name: create
description: Make new capability packages in a work cell from a Create brief, for the user to add to Capabilities.
---

# Create capabilities

The YAML block at the top of brief.md is the user's brief. `title` names what
they want. `make` lists the kinds of package to write (`generator`, `runner`,
`renderer`, `handler`); when it is empty, decide from the answers. `answers`
holds the user's answers to the Create questions, each a `question` and its
`answer`. `related` lists capabilities this channel already has (each a `kind`
and an `id`) that the result should reuse or work with.

Make as few packages as the answers need, and reuse what `related` lists
rather than duplicating it. For a related handler, read its types and skills
with the handler tools when you have them (`list_handlers`, `handler_skills`).

Before writing a package, read its kind's authoring skill in this cell:
`guides/<kind>-authoring/SKILL.md` and its `references/`. Write each package in
its own top-level folder (for example `recipe/`), with exactly one definition
file in that folder: a `.generator`, `.runner`, `.renderer` or `.handler`.
Never put a definition at the cell root or under `guides/`, and do not change
`brief.md` or `guides/`. Write supporting files first and the definition last.
A handler package has no `derived/` folder and no `derived:` line. A runner
names the generator it runs by id.

Check each definition and example file with the tools you have: with a
`validate_file` tool, call it on each one and fix what it reports.

Finish by leaving every package in place and reporting what you made, in the
order the user should add it, and any open questions. The user adds each
package from this cell's Files panel with **Add to Capabilities**, a generator
before the runners that run it. Nothing written here is used until the user
adds it. A new handler is not in use when it is added: the user then builds it
from its card in Capabilities (Build opens a work stack), adds the result back,
and reviews and accepts the build.
