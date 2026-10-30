# Writing a verifier

A handler's `derived/verify.ts` exports one synchronous function. Import only
`collimate/verify`; other imports are refused by the pinned compiler.

```ts
import { parseYaml, validateSchema, type VerifyInput } from 'collimate/verify';
export default function verify(input: VerifyInput) {
  return validateSchema({type: 'object'}, parseYaml(input.text ?? ''));
}
```

Return `{valid: true}` or `{valid: false, error: 'useful explanation'}`. Promises,
thenables and malformed verdicts are invalid. Each file receives a fresh QuickJS
context with a 64 MiB heap and 512 KiB stack. The worker's parent enforces the
absolute deadline across execution, native compilation and result serialization;
the binding's callback-incompatible timer is not used. Verifiers have no
filesystem, network, process or environment API.

The [version 1 interface](interface.md) specifies the input and verdict shapes;
there is no extra version field in the runtime input. Changes to either shape
are interface changes. `input.path` is the cell-relative filename. `input.text` holds text; an opaque
file has `input.bytes: {size, hash}` instead. `input.package` maps package-relative
paths to text (up to 1 MB per file) or `{size, hash}`. For a root file it contains
the root's direct files. `input.cell_paths` lists the entire post-flush cell.
Optional `previous_text` or `previous_bytes` holds this same target file's
committed pre-flush content, absent for a new file. A pending draft is not the
stored baseline. This lets a verifier distinguish removing an existing anchor
from a comment naming an anchor the document never held. Previous content is
part of the verdict cache identity and grants no access to another cell.

The prelude provides `parseYaml`, `parseJson`, `validateSchema`, `tokenizeHtml`,
`matchGlob(path, pattern, root)` and `bundle(files, entrypoint)`. The glob dialect
is the options declaration's dialect; roots are literal. `bundle` accepts a
virtual text-file map, resolves only that package and returns `code`,
`source_hash`, `bundle_hash` and transformed `entrypoint_js`. It invokes the
pinned native compiler and never executes renderer output. Required opaque
content cannot be compiled from metadata. The renderer handler applies schema
validation and static `checkRendererExport(entrypoint_js)` inspection, including
relative imports, rather than loading the renderer in the verifier.

`tokenizeHtml(text)` returns a flat list of `open` (`name`, `attributes`),
`close`, `text` and `comment` tokens. It tokenises as htmlparser2 12 does,
following the WHATWG HTML rules. Tag names are lower-cased, except that SVG
names take their spec case (`foreignObject`). Entities decode in text and in
attribute values. The content of `<script>`, `<style>`, `<xmp>`, `<iframe>`,
`<noembed>`, `<noframes>` and `<plaintext>` is raw text, and that of
`<textarea>` and `<title>` is decoded text, never tags. A heading closes at the
next heading, an `<a>` at the next `<a>`, and a `<p>` or `<li>` at its next
sibling, each with its own `close` token. A nested `<form>` is dropped and
`<image>` reads as `<img>`. `<?…>` and `<!…>` become comments, and `--!>` also
ends a comment. A doctype and a stray `</>` give no token. One run of text can
arrive as several `text` tokens (an entity splits it), so join adjacent ones
before comparing.

The canonical TypeScript prelude in `shared/verify/` is built ahead of time with
`scripts/build_verify_prelude.mjs`. Its self-contained ES module and bundled
dependency licenses travel in the SDK wheel. Runtime verification needs neither
Node nor a dependency download. The renderer handler covers `.renderer` and
`.renderer_example`; preview declarations must name existing cell paths.
