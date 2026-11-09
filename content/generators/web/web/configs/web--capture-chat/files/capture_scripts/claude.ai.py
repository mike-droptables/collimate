"""claude.ai conversation capture — runs on the Capture button press.

Staged as a `capture_scripts` folder by the `web` generator's `capture-chat`
runner. When the current page is claude.ai (or a subdomain), this runs
instead of the generic page snapshot: it scrapes the conversation DOM and saves
`conversation.md`, `messages.json`, and one `artifact_*.<ext>` per inline code
block into this capture's folder in the session's cell.

Self-contained on purpose — capture scripts are `exec`'d, not imported as a
package, so everything it needs lives here.
"""
import json

import gen

# Scrape JS — a zero-arg function run in the page. claude.ai churns its markup
# every few months, so try layered selector strategies and keep whichever
# matched the most messages, except that one whose every message is 'unknown'
# loses to any that labelled a role; the earlier strategy wins a tie. Among
# labelled results the count decides, not how many roles each labelled: a
# capture missing one side shows at a glance, one silently short does not.
_SCRAPE = r"""
() => {
  const USER = '[data-testid="user-message"], .font-user-message';
  const ASSISTANT = '[data-testid="assistant-message"], .font-claude-message';
  const has = (el, sel) => el.matches(sel) || !!el.querySelector(sel);
  const strategies = [
    () => Array.from(document.querySelectorAll(
      '[data-testid="user-message"], [data-testid="assistant-message"]'
    )).map((el) => ({
      role: (el.getAttribute('data-testid') || '').includes('user')
        ? 'user' : 'assistant',
      el,
    })),
    () => {
      const out = [];
      document.querySelectorAll('.font-user-message, .font-claude-message')
        .forEach((el) => out.push({
          role: el.classList.contains('font-user-message') ? 'user' : 'assistant',
          el,
        }));
      return out;
    },
    // [data-test-render-count] elements, one per turn. Render-counts can
    // nest either way. One that holds a render-count carrying a message
    // marker wraps turns (or wraps its own turn's body), so it is not a turn
    // and its text outside the turns is not captured. Of the rest, the
    // outermost is the turn and any render-count inside it is part of it.
    // Each turn is labelled by the marker it holds; when the page's turns
    // hold only one role's marker (the other went stale), an unmarked turn
    // is the other role, and so is any unmarked render-count outside the
    // conversation. With no marker live a wrapper cannot be told from a
    // turn: a render-count around all the turns becomes one 'unknown' turn
    // holding the whole conversation.
    () => {
      const RC = '[data-test-render-count]';
      const marks = (el) => has(el, USER) || has(el, ASSISTANT);
      const counts = Array.from(document.querySelectorAll(RC));
      const wraps = new Set(counts.filter(
        (el) => Array.from(el.querySelectorAll(RC)).some(marks)));
      const turns = counts.filter((el) => {
        if (wraps.has(el)) return false;
        const up = el.parentElement?.closest(RC);
        return !up || wraps.has(up);
      });
      const marked = turns.map((el) => (
        has(el, USER) ? 'user' : has(el, ASSISTANT) ? 'assistant' : ''));
      const live = new Set(marked.filter(Boolean));
      const other = live.size !== 1 ? 'unknown'
        : live.has('user') ? 'assistant' : 'user';
      return turns.map((el, i) => ({ role: marked[i] || other, el }));
    },
  ];
  const rank = (r) => [r.some((m) => m.role !== 'unknown') ? 1 : 0, r.length];
  let messages = [];
  let best = [0, 0];
  for (const strat of strategies) {
    try {
      const r = strat();
      const k = rank(r);
      if (k[0] > best[0] || (k[0] === best[0] && k[1] > best[1])) {
        messages = r;
        best = k;
      }
    } catch (_) { /* next */ }
  }
  // One aggregate source budget keeps even JSON-escaped control text below
  // the transport frame limit. Metadata and collection counts are bounded too.
  let remaining = 1000000;
  let artifactCount = 0;
  let truncated = messages.length > 200;
  const take = (value, limit = remaining) => {
    const text = String(value || '');
    const size = Math.min(limit, remaining);
    if (text.length > size) truncated = true;
    let result = text.slice(0, size);
    if (/[\uD800-\uDBFF]$/.test(result)) { result = result.slice(0, -1); truncated = true; }
    remaining -= result.length;
    return result;
  };
  const metadata = (value, limit) => {
    const text = String(value || '');
    if (text.length > limit) truncated = true;
    let result = text.slice(0, limit);
    // UTF-8 serialization on the host must not receive a split surrogate pair.
    if (/[\uD800-\uDBFF]$/.test(result)) { result = result.slice(0, -1); truncated = true; }
    return result;
  };
  const out = messages.slice(0, 200).map((m) => {
    const artifacts = [];
    m.el.querySelectorAll('pre code').forEach((code) => {
      const cls = code.className || '';
      const match = cls.match(/language-(\S+)/);
      if (artifactCount >= 1000) { truncated = true; return; }
      const content = take(code.innerText || '');
      if (content.trim()) {
        artifacts.push({ language: metadata(match ? match[1] : 'text', 64), content });
        artifactCount++;
      }
    });
    // Remove code from a detached copy only: transcript fences are emitted
    // once from artifacts. Keep paragraph boundaries and the live page intact.
    let prose = m.el.innerText || '';
    if (m.el.querySelector('pre code, code-block')) {
      const copy = m.el.cloneNode(true);
      copy.querySelectorAll('pre, code-block').forEach(block => block.replaceWith('\n'));
      copy.querySelectorAll('p, div, li, br, h1, h2, h3, h4, h5, h6')
        .forEach(block => block.after('\n'));
      prose = copy.textContent || '';
    }
    return { role: m.role, text: take(prose.trim()), artifacts };
  });
  return {
    url: metadata(location.href, 4096),
    title: metadata(document.title, 4096),
    capturedAt: new Date().toISOString(),
    messages: out,
    truncated,
  };
}
"""

_LANG_EXT = {
    "python": "py", "py": "py", "javascript": "js", "js": "js",
    "typescript": "ts", "ts": "ts", "tsx": "tsx", "jsx": "jsx",
    "html": "html", "css": "css", "json": "json", "yaml": "yaml", "yml": "yaml",
    "bash": "sh", "sh": "sh", "shell": "sh", "markdown": "md", "md": "md",
    "sql": "sql", "go": "go", "rust": "rs", "rs": "rs", "java": "java",
}


def _transcript(scrape):
    lines = [
        "# Claude.ai conversation",
        "",
        f"- **URL:** {scrape.get('url', '')}",
        f"- **Captured:** {scrape.get('capturedAt', '')}",
        "",
    ]
    if scrape.get("truncated"):
        lines += ["> Capture truncated at its text or collection limit.", ""]
    for i, msg in enumerate(scrape.get("messages") or [], start=1):
        lines += [f"## {i}. {(msg.get('role') or 'unknown').title()}", "",
                  msg.get("text") or "", ""]
        for art in msg.get("artifacts") or []:
            lines += [f"```{art.get('language') or 'text'}",
                      art.get("content") or "", "```", ""]
    return "\n".join(lines)


async def capture(ctx):
    scrape = await ctx.evaluate(_SCRAPE)
    if not isinstance(scrape, dict):
        gen.log(f"claude capture skipped — got {type(scrape).__name__}")
        return

    msgs = scrape.get("messages") or []
    # The readable transcript first; the rest is detail hanging off it.
    ctx.save("conversation.md", _transcript(scrape))
    ctx.save("messages.json", json.dumps(scrape, indent=2, ensure_ascii=False))
    seen = {}
    for msg in msgs:
        for art in msg.get("artifacts") or []:
            ext = _LANG_EXT.get((art.get("language") or "text").lower(), "txt")
            seen[ext] = seen.get(ext, 0) + 1
            ctx.save(f"artifact_{seen[ext]}.{ext}", art.get("content") or "")

    title = scrape.get("title") or "Claude conversation"
    gen.log(f"captured Claude conversation {title!r} → {ctx.capture_dir or '.'}/ ({len(msgs)} messages)")
