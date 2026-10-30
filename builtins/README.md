# Built-in cells

The handlers and renderers the Collimate app's own code names: the definition
types' handlers, the surfaces the host drives and the plain text floor, plus
the `git-checkout` handler, which types the checkout record the Git generator
writes (`<folder>.git-checkout.json`). It is built in by the owner's decision:
"it is a landmine for the user in terms of navigating and working with git if
an abstraction/tool isn't included... and we expect users to use git often".
Only a built-in handler's verifier and skills are in use without Review and
accept, and its skill is how agents run git on a checkout without `.git`. The app
serves this tree for every channel: its folders are the **Definitions**,
**Live** and **Text** cards at the head of the Capabilities stack, right after
its Manage view. They are never seeded, suggested, exported or copied, and a
channel cannot remove, move, duplicate or edit them. See
[`sdk/shared/docs/how-to-write-a-renderer.md`](../sdk/shared/docs/how-to-write-a-renderer.md)
for renderer authoring and
[`sdk/shared/docs/how-to-write-a-handler.md`](../sdk/shared/docs/how-to-write-a-handler.md)
for handlers.

## Layout

The same parent-folders-are-cells layout as `content/renderers/` (see
`collimate._host.packaging.layout`); each `cell.yaml` names its card.

```
builtins/
├── definitions/   — card "Definitions": the generator, renderer, runner, handler,
│                    options and git-checkout handlers, and renderer-example
├── live/          — card "Live": run, assistant, terminal, website, webview
└── text/          — card "Text": text, the floor for every text file
```

The handlers own the reserved types (`collimate.handlers.RESERVED_TYPES`): a
channel's handler cannot claim one, and a channel's renderer cannot use a
built-in renderer id or a surface extension. A channel can add examples to a
built-in handler and regenerate its skills: Regenerate skills on its card opens a
work stack with the `handler` handler's `regenerate` skill as its brief, and the
regenerated skills come back through that stack's Files panel for review. The
handler's definition, guidance, verifier and shipped examples stay as shipped
here.

The app names these packages in one list, `api/builtins.json`: each one's kind,
id and content hash. A tree that differs from it (a package added, removed or
changed) refuses to start. After any edit here, run
`PYTHONPATH=api uv run python build/sync_builtins.py` and commit the list with
the edit. After changing a canonical guide or a handler's hand-written
`derived/` files, run `python3 sdk/scripts/sync_skill_references.py` first: it
regenerates the reference mirrors and the `derived:` hashes together. The
upstream credits for these renderers are in
[`content/renderers/CREDITS.md`](../content/renderers/CREDITS.md).
