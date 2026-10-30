# SDK guides

These are the canonical guides for the SDK contracts and package formats. A
channel uses the generators, runners, renderers and handlers in the cells of
its Capabilities stack; packages are written in ordinary work cells and added
from there. Authoring guides travel in the generated skill references of each shipped
handler's build (`derived/skills/**/references/`). The handler authoring and build skills carry separate recipes and
references. The Guide preset carries only the overview and philosophy for user
orientation. Run `python3 scripts/sync_skill_references.py` after changing a
canonical guide; it regenerates reference bytes and the built-in handlers'
`derived:` hashes together. `tests/test_skill_references.py` pins both.

- [Overview](collimate-overview.md)
- [Why Collimate](why-collimate.md)
- [Interface contracts, version 1](interface.md)
- [Generator authoring](how-to-write-a-generator.md)
- [Runner authoring](how-to-write-a-runner.md)
- [Renderer authoring](how-to-write-a-renderer.md)
- [Handler authoring](how-to-write-a-handler.md)
- [Split options](how-to-write-options.md)
- [Verifier authoring](how-to-write-a-verifier.md)
- [API reference](api-reference.md)
- [Testing](testing-generators.md)
- [Best practices](authoring-best-practices.md)
- [Generic surface design](generic-surface-design.md)
- [Agent runtime design](agent-runtime.md)
