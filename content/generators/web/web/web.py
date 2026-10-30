# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp", "pyyaml"]
# ///
"""Web — daemon-shaped live browser session.

Writes the run's browser tabs as ``tab-<n>.webview`` files in its run folder
(``gen.update_ui_file``) so the webview renderer (address bar + page canvas) shows
each one, and runs a session daemon behind them (see ``_web_shared/``). The
first tab opens on the prompt's URL; the URL is optional, and without one the
tab opens on a blank page and the address bar is how the runner names a
site — the tab's canvas shows an Enter page form until it is on one. Each
tab's toolbar carries Capture and New tab; New tab opens another tab (blank)
and shows it. Stop ends the session — the daemon deletes every tab file on
the way out, so the cell is left holding its captures and nothing live.

There is no browser process anywhere: the desktop app's own native guest view
loads, renders and navigates the page, and the daemon reaches it over the
control WS's RPC. That is the only way a session runs fully live — the
tab file says ``requires: ["nativeGuests"]`` unconditionally. That gates the
renderer's mode, not whether the cell renders: the same cell opened in a
browser tab falls back to a best-effort iframe, with a working address bar
and a banner naming what is degraded (framing refusals, no persistent login),
and the capture verbs refuse there because there is no guest to serve them
(the tab's Capture still shows, and every press fails).

**Capture.** A tab's own toolbar Capture emits a ``capture`` action whose
source is that tab's file, so the press acts on that tab; the generator
dispatches it by the tab's CURRENT page host (so it re-evaluates as you
navigate):

  - a runner may provide per-host scripts via the ``capture_scripts``
    config folder — a flat ``<host-pattern>.py`` per host (``_default.py``
    = fallback); the matching one runs against the live session (see
    :func:`_select_capture_script` / :func:`_run_capture_script` and the
    generator's ``samples/capture_scripts`` for the ``capture(ctx)``
    contract);
  - with no matching script, the built-in generic capture runs: ``page.md`` /
    ``page.html`` / ``screenshot.png``.

Either way the capture writes into THIS cell — ``ctx.save(relpath, content)``
puts each file under the press's own ``<host>-<YYYYMMDD-HHMMSS>`` folder — so
a browsing session accumulates its snapshots in one place instead of scattering
cells across the canvas.

The login bucket is the ``profile`` setting: it names the guest's persistent
``persist:collimate-<name>`` session partition (every tab of a run shares it),
so signing in once through the cell keeps the login across runs, cells sharing
a name share cookies, and a different name is an independent session that can
run at the same time.

The ``device`` setting is what each tab presents as: Desktop (the desktop
app's own browser), Phone or Tablet (that device's user agent and a mobile
viewport of its width, letterboxed on the cell). Each tab's toolbar carries a
Desktop / Phone / Tablet toggle that switches that tab alone, in place.
"""
from __future__ import annotations

import inspect
import os
import sys
import traceback
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

# The shared _* cell-folders are SIBLINGS at run time (materialised into
# the cell); in the repo/tests they live at the asset root, one level above
# this generator's cell parent folder. Insert both so the lazy
# ``_web_shared.daemon`` import in ``run()`` resolves. (The import itself
# is deferred — it pulls in aiohttp, which only the real run needs, so the
# module stays importable for host-free tests of the helpers below.)
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))
sys.path.insert(0, os.path.dirname(os.path.dirname(_here)))

import gen  # noqa: E402


# ── Generic page capture ────────────────────────────────────────────────

# Page-scrape JS. Runs inside the page via ``ctx.evaluate(...)``. Site-
# agnostic on purpose — innerText + anchors works everywhere, which is what
# lets this be the one built-in; anything site-specific is a capture script.
_PAGE_SCRAPE_JS = r"""
() => {
  let truncated = false;
  const cap = (value, limit) => {
    const text = String(value || '');
    if (text.length > limit) truncated = true;
    let result = text.slice(0, limit);
    // UTF-8 serialization on the host must not receive a split surrogate pair.
    if (/[\uD800-\uDBFF]$/.test(result)) { result = result.slice(0, -1); truncated = true; }
    return result;
  };
  const meta = document.querySelector('meta[name="description"]');
  const links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href;
    if (!/^https?:/.test(href)) continue;
    const text = cap((a.innerText || '').trim().replace(/\s+/g, ' '), 120);
    if (!text) continue;
    links.push({ text, href: cap(href, 4096) });
    if (links.length >= 100) break;
  }
  return {
    url: cap(location.href, 4096),
    title: cap(document.title, 4096),
    description: cap(meta ? meta.content : '', 8192),
    capturedAt: new Date().toISOString(),
    text: cap(document.body ? document.body.innerText : '', 1000000),
    links,
    truncated,
  };
}
"""

# Bound the serialized DOM inside the guest, before its single RPC reply.
_PAGE_HTML_JS = r"""
() => {
  const html = document.documentElement?.outerHTML || '';
  let content = html.slice(0, 1000000);
  if (/[\uD800-\uDBFF]$/.test(content)) content = content.slice(0, -1);
  return { html: content, truncated: html.length > content.length };
}
"""


def _md_link(text: Any, href: Any) -> str:
    # Escape the link text's brackets and percent-encode the destination's
    # parens/spaces, so an unbalanced ']' or ')' cannot end the link early
    # (and the plain-regex markdown renderer, which stops at the first ')',
    # still reads the whole URL).
    text = str(text or "").replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    href = str(href or "").replace("(", "%28").replace(")", "%29").replace(" ", "%20")
    return f"[{text}]({href})"


def _format_page(scrape: dict[str, Any]) -> str:
    lines: list[str] = [
        f"# {scrape.get('title') or 'Untitled page'}",
        "",
        f"- **URL:** {scrape.get('url', '')}",
        f"- **Captured:** {scrape.get('capturedAt', '')}",
    ]
    desc = " ".join(str(scrape.get("description") or "").split())
    if desc:
        lines.append(f"- **Description:** {desc}")
    if scrape.get("truncated"):
        lines += ["", "> Capture truncated at its text or metadata limit."]
    lines += ["", "## Text", "", scrape.get("text") or "(no text content)"]
    links = scrape.get("links") or []
    if links:
        lines += ["", "## Links", ""]
        lines += [f"- {_md_link(l.get('text'), l.get('href'))}" for l in links]
    lines.append("")
    return "\n".join(lines)


async def _capture_page(ctx: Any) -> None:
    # Stage pixels before page text/DOM: a later scrape failure must still
    # leave this artifact for the daemon's partial-output flush.
    try:
        ctx.save("screenshot.png", await ctx.screenshot_png())
    except Exception as exc:
        gen.log(f"screenshot failed: {exc}")
    scrape = await ctx.evaluate(_PAGE_SCRAPE_JS)
    if not isinstance(scrape, dict):
        gen.log(f"capture skipped — scrape returned {type(scrape).__name__}")
        return
    # A capture is a bounded snapshot into this press's own folder:
    # readable page.md plus the bounded DOM and a screenshot. Each heavy
    # artifact is best-effort — a failure logs and is skipped rather than
    # losing the whole capture.
    ctx.save("page.md", _format_page(scrape))
    try:
        snapshot = await ctx.evaluate(_PAGE_HTML_JS)
        html = snapshot["html"]
        if snapshot["truncated"]:
            html += "\n<!-- Snapshot truncated at 1000000 UTF-16 code units. -->"
            gen.log("html snapshot truncated at 1000000 UTF-16 code units")
        ctx.save("page.html", html)
    except Exception as exc:
        gen.log(f"html snapshot failed: {exc}")
    title = (scrape.get("title") or scrape.get("url") or "page").strip()
    gen.log(f"captured page → {ctx.capture_dir}/ ({title[:60]})")


# ── URL-host dispatch ───────────────────────────────────────────────────

def _hostname(url: str) -> str:
    # Current guest URLs are already absolute. Match capture-folder parsing:
    # non-hierarchical/blank URLs have no host; do not invent a scheme here.
    try:
        return (urlsplit(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _host_matches(host: str, domain: str) -> bool:
    domain = (domain or "").strip().lower().lstrip(".").rstrip(".")
    try:
        # The guest reports IDN hosts in punycode; match a Unicode stem too.
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        pass
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


# ── Capture-script dispatch ─────────────────────────────────────────────

def _select_capture_script(
    scripts: dict[str, Any], host: str,
) -> tuple[str, str] | None:
    """Pick the capture script for ``host`` from a ``capture_scripts`` folder.

    ``scripts`` is ``{relpath: str|bytes}`` (from ``gen.config_folder``). Only
    flat top-level ``<pattern>.py`` files are considered — the stem is a host
    pattern matched by :func:`_host_matches` (so ``claude.ai.py`` covers
    ``claude.ai`` and any subdomain). The longest matching stem wins;
    ``_default.py`` is the fallback when nothing else matches. Returns
    ``(name, source)`` or ``None`` (→ the built-in page capture)."""
    best: tuple[int, str, str] | None = None
    default: tuple[str, str] | None = None
    for rel, content in scripts.items():
        if "/" in rel or "\\" in rel or not rel.endswith(".py"):
            continue
        if isinstance(content, bytes):
            try:
                content = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
        if not isinstance(content, str):
            continue
        stem = rel[:-3]
        if stem == "_default":
            default = (rel, content)
            continue
        if host and _host_matches(host, stem):
            if best is None or len(stem) > best[0]:
                best = (len(stem), rel, content)
    if best is not None:
        return best[1], best[2]
    return default


async def _run_capture_script(name: str, source: str, ctx: Any) -> None:
    """Execute one capture script against the live session.

    The script is a self-contained module defining ``async def capture(ctx)``;
    it uses ``ctx`` (``.url`` / ``await .evaluate(js)`` / ``await .html()`` /
    ``await .screenshot_png()``) to scrape and ``ctx.save(relpath, content)``
    to write the result into this cell, under the press's own capture folder.
    A script that fails to load (or has no ``capture``) logs and does nothing.
    A failure inside ``capture(ctx)`` — ``SystemExit`` included — is logged
    with the script name and traceback and surfaces to the daemon as an
    ordinary ``RuntimeError``, so the press counts as failed and the session
    never ends."""
    namespace: dict[str, Any] = {"__name__": f"capture_script:{name}",
                                 "__file__": name}
    try:
        code = compile(source, f"<capture_scripts/{name}>", "exec")
        exec(code, namespace)  # noqa: S102 — runner-provided, runs under the run's grant
    except (Exception, SystemExit) as exc:
        gen.log(f"capture script {name!r} failed to load: {exc!r}")
        return
    fn = namespace.get("capture")
    if not callable(fn):
        gen.log(f"capture script {name!r} defines no capture(ctx) function")
        return
    try:
        result = fn(ctx)
        if inspect.isawaitable(result):
            await result
    except (Exception, SystemExit) as exc:
        gen.log(f"capture script {name!r} failed: {exc!r}\n{traceback.format_exc()}")
        raise RuntimeError(f"capture script {name!r} failed: {exc}") from exc


def make_capture_dispatcher(
    capture_scripts: dict[str, Any],
) -> Callable[[Any], Awaitable[None]]:
    """Build the single ``on_capture`` callback the daemon calls on each
    Capture press. It dispatches by the CURRENT page host: a matching
    ``capture_scripts`` script wins; otherwise the built-in generic page
    snapshot runs.
    """
    async def _on_capture(ctx: Any) -> None:
        host = _hostname(getattr(ctx, "url", "") or "")
        selected = _select_capture_script(capture_scripts, host)
        if selected is not None:
            name, source = selected
            gen.log(f"capture via script {name!r} (host {host or '?'})")
            await _run_capture_script(name, source, ctx)
            return
        await _capture_page(ctx)

    return _on_capture


async def run() -> None:
    if gen.grant().internet == "none":
        message = "Web cannot start with internet: none; set internet: full to open a web session."
        gen.log(message)
        raise RuntimeError(message)
    cfg = gen.config("settings.set_yaml")
    profile_name = str(cfg.get("profile") or "default").strip() or "default"
    # What the tabs open as; each tab's toolbar toggle switches it in place.
    device = str(cfg.get("device") or "desktop").strip().lower() or "desktop"

    # The starting URL is optional: blank opens the session on a blank page,
    # with the address bar as the way in.
    url = (gen.prompt or "").strip()

    # Per-host capture scripts a runner staged (empty when none) — dispatched by
    # the CURRENT host on each Capture press, so it re-evaluates as you browse.
    on_capture = make_capture_dispatcher(gen.config_folder("capture_scripts"))

    # aiohttp is heavy and only the real run needs it — import the daemon here
    # (not at module load) so the helpers above stay host-free-testable.
    from _web_shared.daemon import run_web_session  # noqa: E402

    await run_web_session(
        url=url,
        cell_name="Web",
        on_capture=on_capture,
        capture_button_label="Capture",
        profile_name=profile_name,
        device=device,
    )


if __name__ == "__main__":
    gen.main(run)
