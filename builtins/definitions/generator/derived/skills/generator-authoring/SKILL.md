---
name: generator-authoring
description: Write and validate generator packages.
---

# Generator packages

Write generator source, main settings schema and named configs first, then the definition; validate before asking the user to add it to Capabilities.

Read the canonical guides in `references/`. Write supporting files first and the definition last, then call `validate`. A work-cell definition is inert until the user adds it with Add to Capabilities.

When the generator needs credentials or settings from the Key manager, give each account role a shipped kind or a structure the keystore already has (`gen.keystore_structures()`): match an existing structure before declaring a new one. A generator never writes the keystore; a new structure is one the user builds by hand (see the generator guide's Custom structures).
