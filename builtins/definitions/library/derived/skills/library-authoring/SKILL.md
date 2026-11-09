---
name: library-authoring
description: Write and validate shared library packages.
---

# Library packages

A library is code several generators share: the folder `<id>/` at the top of its cell, named by its id (an underscore name such as `_my_helpers`, which is also its import name), holding `<id>.library` and the code. A generator's cell names it under `shared:` in its `cell.yaml`, and the host stages it beside the generator when a run starts.

Read the canonical guides in `references/`. Write the code first and the definition last, then call `validate`. A work-cell definition is inert until the user adds it with Add to Capabilities; add a library before the generator that names it.
