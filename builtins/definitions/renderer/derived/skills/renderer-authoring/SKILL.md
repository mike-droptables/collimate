---
name: renderer-authoring
description: Write and verify renderer packages and preview declarations.
---
Read references/how-to-write-a-renderer.md and references/api-reference.md.
Use inline styles in shadow DOM. Write code first, then the .renderer definition.
The entrypoint and relative imports stay inside the package; export a
statically readable defineRenderer({ component }) result. Preview declarations
name existing renderer and sample paths. Verifier authoring is described in
references/how-to-write-a-verifier.md. Compilation never executes the renderer.
