# Actual-app shipped-renderer rollout gate

The test loads the built workbench from its actual API origin and opens each
shipped renderer using the server's durable file display descriptor. Source,
SDK runtime, cache and CDN modules are never replaced. Every renderer has a
meaningful DOM/resource assertion; disappearing host fallback overlays is not
readiness. The canonical SDK roster must exactly match the fixture matrix (currently 20 renderers).

Use the phase-approved vendor/build first. Start the helper from the repo root:

```sh
PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring tmp/p1/api-venv/bin/python \
  api/tests/helpers/renderer_rollout_server.py --port 5113 --page-port 5114 \
  --data "$PWD/tmp/p3/rollout/data" --static "$PWD/frontend/dist"
```

The disposable helper requires ffmpeg to produce a valid WebM; a missing
tool/codec is a setup failure, never a renderer pass or skip. Ports 5113 and
5114 must both be available. The second serves only a controlled `/page`,
because webview correctly refuses app-origin and same-port loopback aliases.
Its artifact uses the producer's actual `nativeGuests` capability name.
The helper supplies only
synthetic local page/media/socket resources. WAV/WebM/PNG/PDF are uploaded to
actual CAS before cells are created with the ordinary API. Terminal uses a
controlled socket replay; assistant uses transcript-only state.
The `.renderer_example` fixture points at an unapproved local native renderer
package and an ordinary text sample in the same work cell. Its inner sealed
frame must render a heading whose text comes from the actual sample read through
the preview API relay, and its sandbox must be `allow-scripts`. The outer shipped
preview module still requires a prebuilt cache entry; its local candidate uses
the real browser compiler. Cold and warm app mounts both check inner content.
No preview source, compiler, routes or runtime responses are replaced.

These establish shipped-module readiness without claiming real generator or
provider behavior. PDF resource/object assertions do not claim a headless
browser's internal PDF viewer painted its page. The test separately requires
the actual capability response to contain PDF bytes and the PDF content type.
Webview exercises the browser
fallback; desktop native guests require a separate actual-shell check.

Then, with the phase's Node toolchain on PATH:

```sh
cd frontend
npx playwright test -c playwright.renderer.config.ts p3-renderer-rollout.spec.ts
```

One worker, no retries/skips. Report every case, source/cache identity, failures
and resource dependencies. JSON, traces and screenshots are under ignored
`tmp/p3/rollout`. No shared `api/static` mutation is needed. Stop only the helper
process you started. The P3 step-one bundle contract and cold/warm cache pins
are coordinated with the root implementation brief; this infrastructure alone
does not assert phase acceptance or the later host flush/keyboard contract.

Skin baselines run against the same helper:

```sh
npx playwright test -c playwright.renderer.config.ts skin-baselines.spec.ts
```

The roster is read from the helper's `/__renderer_rollout/roster` when the
file is collected (the SDK's own definition discovery), so a renderer the SDK
ships without a case in `fixtures.ts` fails, and an unreachable helper fails
collection. Each renderer mounts through the rollout projection and is captured
as its cell in `night`, `night-cool` and `paper`, switched with
`PUT /api/channels/{channel}/settings` (`theme.activeThemeId`) and a reload.
Every frame that defines `--sheet` must carry the skin's value first. Captures
wait for three identical frames half a second apart with focus released (a
focused terminal blinks). A missing baseline is written from that capture and
compared in the same run; later runs compare. Baselines live in
`skin-baselines.spec.ts-snapshots/`. The per-pixel threshold (0.01) sits below
one skin step, so a swapped or literal surface fails; a last case requires
exactly one baseline per renderer and skin. To re-baseline after an intended
restyle, delete the affected PNGs and run once.

The iframe host's theme round trip (R01) runs against the same helper:

```sh
npx playwright test -c playwright.renderer.config.ts theme-roundtrip-iframe.spec.ts
```

Every shipped `embed: iframe` renderer in the roster mounts in `night`, then
the skin is changed live twice (`paper`, then `night-cool`) through Settings →
Themes, with no reload. Each change must reach every `sandbox.html` frame
(`data-theme`, `data-scheme` and `--sheet-2` verbatim) and repaint the
renderer's ground in the new skin. The native host's half is
`src/components/__browser__/theme-roundtrip-native.browser.test.tsx`.

The page renderer's DarkReader on the skin (R04 W2b) runs against the same
helper, once its templates carry the current `html` renderer:

```sh
npx playwright test -c playwright.renderer.config.ts html-darkreader-live.spec.ts
```

The html renderer opens with its style theme set to `darkened` (the channel's
`renderer_styles.html`, PUT with the skin) in `night`. The srcDoc must enable DarkReader on night's `--sheet`
and `--ink` (built from the host's record, never `DarkReader.enable({})` from
the placeholder), DarkReader's neutral ground and text must be those colours,
and the frame backing behind the page must be the sheet. The skin then changes
to `night-cool` through Settings → Themes: the same page document (a marker
set in it survives, the srcDoc is unchanged) must repaint onto night-cool's
sheet and ink through the posted `collimate:darkreader` message.

The libraries' own colours (R02 and R05 W2b) are checked by reading what they
paint, in `night`, `night-cool` and `paper`, against the same helper:

```sh
npx playwright test -c playwright.renderer.config.ts library-colours-live.spec.ts
```

The skin baselines' pixel budget admits a few recoloured glyphs, so they
cannot tell a library's own keyword colour from the skin's. Here Monaco's
keyword, number and comment, and a fenced JavaScript block's in milkdown, must
be drawn in the `syntaxPalette` colours that the served `/api/sdk/react.js`
derives (in the renderer's own frame) from the skin's tokens. Excalidraw's
scene canvases must be transparent over the frame's `--canvas`, and its
islands must be the skin's `--sheet-2`.

The Step 2 worker gate runs separately:

```sh
npx playwright test -c playwright.renderer.config.ts p3-worker.spec.ts
```

Its four cases edit the exact text package through conditional unified file mutations, require
an initial cache miss, compile native and iframe modules in the actual local
esbuild-wasm worker, compare the published bytes and source hash with the SDK Node
compiler, and reload without creating another worker. Syntax-error cases pin
the visible compiler diagnostic, absent cache entry and correction/retry.
Unique authored comments prevent earlier successful runs from prebuilding the
same global source identity. No compiler/source/cache responses are replaced.
