"""Sample per-host capture script for the `web` generator.

Docs-only starter (this whole `samples/` tree is never staged into a run —
it just shows the shape). Copy it into a runner's
`<runner-package>/files/capture_scripts/<host>.py` and adapt the scrape.

Filename convention: the name minus `.py` is a host pattern. `example.com.py`
matches `example.com` and any subdomain; `_default.py` is the fallback.

Contract: define `async def capture(ctx)`. `ctx` gives you the live page —
`ctx.url`, `await ctx.evaluate(js)`, `await ctx.html()`,
`await ctx.screenshot_png()` — and `ctx.save(relpath, content)` writes your
result into the session's own cell, under this capture's folder.
"""
import gen

# A zero-arg JS function, run in the page. Return only JSON-safe values.
_SCRAPE = r"""
() => ({
  url: location.href,
  title: document.title,
  heading: (document.querySelector('h1') || {}).innerText || '',
  text: (document.body ? document.body.innerText : '').slice(0, 20000),
})
"""


async def capture(ctx):
    data = await ctx.evaluate(_SCRAPE)
    if not isinstance(data, dict):
        gen.log("example.com capture: page returned no data")
        return

    title = (data.get("title") or data.get("url") or "page").strip()
    body = [
        f"# {data.get('heading') or title}",
        "",
        f"- **URL:** {data.get('url', '')}",
        "",
        "## Text",
        "",
        data.get("text") or "(no text content)",
        "",
    ]
    # The readable page first.
    ctx.save("page.md", "\n".join(body))

    # Heavy artifacts are best-effort — never lose the whole capture to one.
    try:
        ctx.save("screenshot.png", await ctx.screenshot_png())
    except Exception as exc:
        gen.log(f"screenshot failed: {exc}")

    gen.log(f"captured example.com page → {ctx.capture_dir}/ ({title[:60]})")
