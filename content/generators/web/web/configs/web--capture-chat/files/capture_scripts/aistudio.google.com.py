"""Google AI Studio conversation capture — runs on the Capture button press.

Staged as a `capture_scripts` folder by the `web` generator's `capture-chat`
runner. When the current page is aistudio.google.com (or a subdomain), this
runs instead of the generic page snapshot: it scrapes the conversation DOM and
saves `conversation.md`, `messages.json`, and one `artifact_*.<ext>` per inline
code block into this capture's folder in the session's cell.

Self-contained on purpose — capture scripts are `exec`'d, not imported as a
package, so everything it needs lives here.
"""
import json

import gen

# Scrape JS — a zero-arg function run in the page. AI Studio's chat UI is built
# on Angular web components: turn blocks are `<ms-chat-turn>`, user text lives
# in `<ms-text-chunk>` slots, model output renders through `<ms-cmark-node>`.
# Google has rewritten the Studio UI three times in two years, so try layered
# selector strategies and take the first strategy, in precision order, that
# matched anything.
_SCRAPE = r"""
() => {
  const strategies = [
    // 1. Modern: each turn is an <ms-chat-turn> custom element with a
    //    [data-turn-role] or `.user-prompt` / `.model-prompt` class signal.
    () => Array.from(document.querySelectorAll('ms-chat-turn')).map((el) => {
      let role = 'unknown';
      if (el.classList.contains('user-prompt-container')
          || el.querySelector('.user-prompt')) role = 'user';
      else if (el.classList.contains('model-prompt-container')
          || el.querySelector('.model-prompt')) role = 'assistant';
      const attr = el.getAttribute('data-turn-role');
      if (attr) role = attr.toLowerCase().includes('user') ? 'user' : 'assistant';
      return { role, el };
    }),
    // 2. Older: alternating .user-prompt-container / .model-prompt-container.
    () => Array.from(document.querySelectorAll(
      '.user-prompt-container, .model-prompt-container'
    )).map((el) => ({
      role: el.classList.contains('user-prompt-container') ? 'user' : 'assistant',
      el,
    })),
    // 3. Last ditch: every outermost <ms-cmark-node> / <ms-text-chunk> (nested
    //    matches dropped) is a message; the roles are a best-effort guess —
    //    <ms-text-chunk> is taken as user, <ms-cmark-node> as assistant.
    () => Array.from(document.querySelectorAll('ms-cmark-node, ms-text-chunk'))
      .filter((el) => !el.parentElement?.closest('ms-cmark-node, ms-text-chunk'))
      .map((el) => ({
        role: el.tagName.toLowerCase() === 'ms-text-chunk' ? 'user' : 'assistant',
        el,
      })),
  ];
  let messages = [];
  for (const strat of strategies) {
    try {
      const r = strat();
      if (r.length) { messages = r; break; }
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
    m.el.querySelectorAll('pre code, code-block').forEach((code) => {
      // A <code-block> wrapping <pre><code> is collected through the inner one.
      if (code.tagName.toLowerCase() === 'code-block'
          && code.querySelector('pre code')) return;
      const cls = code.className || '';
      const match = cls.match(/language-(\S+)/);
      let language = match ? match[1] : '';
      if (!language) {
        // AI Studio sometimes labels the language on a sibling header.
        const header = code.closest('code-block')
          ?.querySelector('.code-block-decoration-header, .code-block-language')
          ?.innerText || '';
        // First token only: the header also holds button/icon text.
        language = (header.trim().split(/\s+/)[0] || 'text').toLowerCase();
      }
      if (artifactCount >= 1000) { truncated = true; return; }
      const content = take(code.innerText || '');
      if (content.trim()) {
        artifacts.push({ language: metadata(language, 64), content });
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


def _lang(art):
    # First token only, so a stray multi-word label can't break the fence.
    parts = (art.get("language") or "").split()
    return parts[0].lower() if parts else "text"


def _transcript(scrape):
    lines = [
        "# Google AI Studio conversation",
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
            lines += [f"```{_lang(art)}",
                      art.get("content") or "", "```", ""]
    return "\n".join(lines)


async def capture(ctx):
    scrape = await ctx.evaluate(_SCRAPE)
    if not isinstance(scrape, dict):
        gen.log(f"AI Studio capture skipped — got {type(scrape).__name__}")
        return

    msgs = scrape.get("messages") or []
    # The readable transcript first; the rest is detail hanging off it.
    ctx.save("conversation.md", _transcript(scrape))
    ctx.save("messages.json", json.dumps(scrape, indent=2, ensure_ascii=False))
    seen = {}
    for msg in msgs:
        for art in msg.get("artifacts") or []:
            ext = _LANG_EXT.get(_lang(art), "txt")
            seen[ext] = seen.get(ext, 0) + 1
            ctx.save(f"artifact_{seen[ext]}.{ext}", art.get("content") or "")

    title = scrape.get("title") or "AI Studio conversation"
    gen.log(f"captured AI Studio conversation {title!r} → {ctx.capture_dir or '.'}/ ({len(msgs)} messages)")
