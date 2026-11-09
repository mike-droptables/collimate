# `capture_scripts/` — per-host page-capture scripts

This is a **sample** (docs-only — never staged into a run). It shows the shape
of the `capture_scripts` config folder a runner provides for the `web`
generator. Real scripts live under `<runner-package>/files/capture_scripts/` in a
runner's cell and are staged to the run before it starts.

## What it does

When a tab's current page host matches and the user presses **Capture** (on
the tab's toolbar), the `web` generator runs the matching script against that
tab's live session (instead of the built-in generic page snapshot).

The session is the desktop app's own native guest view, reached over the session RPC
— the script never has to care. `ctx` is the whole surface: it hands the same
handle to every script, and `ctx.evaluate(...)` runs your JS in the live page
and returns its JSON-safe value.

## Naming convention — dispatch by host

One flat `<host-pattern>.py` file per host. The filename (minus `.py`) is a
host pattern that matches its own domain **and any subdomain**:

| File | Matches |
|---|---|
| `claude.ai.py` | `claude.ai`, `www.claude.ai`, `foo.claude.ai` |
| `docs.example.com.py` | `docs.example.com` and its subdomains |
| `_default.py` | anything with no more specific match (fallback) |

The **longest** (most specific) matching stem wins. If nothing matches and there
is no `_default.py`, the generator falls back to its built-in capture. Only
flat top-level `.py` files are considered — a script must be self-contained
(no sibling-module imports; it is `exec`'d, not imported as a package).

## The `capture(ctx)` contract

Each script defines one coroutine:

```python
async def capture(ctx):
    ...
```

`ctx` is the live capture handle:

| Member | Meaning |
|---|---|
| `ctx.url` | the page URL when the capture started (str, a snapshot) |
| `await ctx.current_url()` | the guest's live URL, fetched from the client (str) |
| `await ctx.navigate(url)` | point the guest at `url` |
| `await ctx.evaluate(js_fn_source)` | run a zero-arg JS function `() => {…}` in the page, get its JSON-safe return value |
| `await ctx.html()` | the page's serialized DOM (str) |
| `await ctx.screenshot_png()` | a PNG screenshot (bytes) |
| `ctx.save(relpath, content)` | write one file of this capture (`str` or `bytes`) |
| `ctx.capture_dir` | this capture's folder name, `<host>-<YYYYMMDD-HHMMSS>` |

## Where a capture lands

Into the session's **own cell**, not a new one. Every `ctx.save(…)` goes under
`ctx.capture_dir`, so each Capture press is one dated folder and pressing it
ten times leaves ten folders side by side. `relpath` is relative to that folder
(nested paths are fine; a `..` segment is refused), so a script never has to
worry about colliding with an earlier press — or reaching outside its own
folder. The saves are staged; the generator flushes them when your
`capture(ctx)` returns, so one capture appears as one folder.

Save a readable page (`page.md`, `conversation.md`, …) alongside any raw
files; the generator never pins or opens a capture for you.

`import gen` for `gen.log(…)` progress lines. Keep failures contained — a
raising script just logs and never takes the browser session down.

See `example.com.py` in this folder for a minimal working example.
