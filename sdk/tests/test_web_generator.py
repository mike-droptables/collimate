# SPDX-License-Identifier: MIT
"""Tests for the `web` generator and the session machinery behind it.

The real run drives a live page in the desktop client's own native guest view —
not host-free territory. There is one way to run a session, so there is one of
everything below. What IS host-free and worth pinning:

  * the capture-script dispatch convention (host → script, `_default`
    fallback), and that a dispatched script — including the two the `web`
    generator's runners actually ship — writes its files into THIS cell, under the press's
    own `<host>-<timestamp>` folder;
  * the control-WS RPC that reaches the guest, driven end to end against an
    in-process fake client socket — including that the `capture(ctx)` contract
    saves the same files over it;
  * the `tab-<n>.webview` tab file (v4) as a golden, with its device and
    viewport, that the keys the deleted modes carried (and the shell-composed
    user agent) never come back, and that a session without a site carries
    `url: ""`;
  * the session lifecycle: the first tab is a UI file in the run folder,
    flushed then revealed; a tab's toolbar New tab adds the next one; a Capture
    acts on the tab it was clicked for; a device toggle rewrites only the
    tab it was pressed on; Stop leaves the run loop and deletes
    every tab — and the closing log line tells the truth about how the run
    ended and what it kept;
  * the generator's surface: `run()`'s call into the daemon, the settings, the
    manifest, the shipped runners.

`web.py` imports its aiohttp runtime lazily inside `run()`, so the module
imports cheaply here; the `_web_shared` tests below need aiohttp and skip
without it.
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.util
import json
import os
import re
import sys

import pytest
import yaml

from collimate.lint.core import Level, lint_path
from collimate.testing import run_generator_callable as _run_generator_callable

from tests._workspace import GENERATORS


_RESULT_UPDATES = {}


def run_generator_callable(*args, **kwargs):
    from unittest.mock import patch
    import collimate.testing as testing
    made = []
    original = testing.make_test_bridge

    def record(*args, **kwargs):
        bridge = original(*args, **kwargs)
        made.append(bridge)
        return bridge

    with patch.object(testing, "make_test_bridge", record):
        result = _run_generator_callable(*args, **kwargs)
    _RESULT_UPDATES[id(result)] = made[-1].updates if made else []
    return result


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB_DIR = os.path.join(GENERATORS, "web", "web")
_WEB_PY = os.path.join(_WEB_DIR, "web.py")
_RUNNER_DIR = os.path.join(GENERATORS, "web", "web", "configs", "web--capture-chat")


def _load_web():
    """Import generators/web/web/web.py under a private name (no aiohttp needed —
    the runtime import is deferred to run())."""
    spec = importlib.util.spec_from_file_location("_web_under_test", _WEB_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


web = _load_web()


def _shared():
    """The `_web_shared` package (protocol / capture / daemon), imported off
    the generators/ asset root the way a real run does. Needs aiohttp — skipped
    where the optional dep isn't installed."""
    pytest.importorskip("aiohttp")
    root = str(GENERATORS)
    if root not in sys.path:
        sys.path.insert(0, root)
    from _web_shared import capture, daemon, protocol

    return protocol, capture, daemon


# ---------------------------------------------------------------------------
# Fake capture context — the duck-typed handle a capture callback receives.
# ---------------------------------------------------------------------------

# What the daemon names one capture's folder; fixed here so the tests can
# read the files back by path.
_DIR = "example.org-20260101-000000"


class FakeCtx:
    def __init__(self, url, *, scrape=None, html="<html></html>", png=b"\x89PNG\r\n",
                 capture_dir=_DIR):
        self.url = url
        self.capture_dir = capture_dir
        self.saved: list[str] = []
        self._scrape = scrape if scrape is not None else {}
        self._html = html
        self._png = png
        self.evaluated: list[str] = []

    def save(self, relpath, content):
        import gen

        path = f"{self.capture_dir}/{relpath}" if self.capture_dir else relpath
        gen.update_file(path, content)
        self.saved.append(path)

    async def evaluate(self, js):
        self.evaluated.append(js)
        if js == web._PAGE_HTML_JS:
            return {"html": self._html, "truncated": False}
        return self._scrape

    async def html(self):
        return self._html

    async def screenshot_png(self):
        return self._png


_CLAUDE_SCRAPE = {
    "url": "https://claude.ai/chat/abc",
    "title": "My chat",
    "capturedAt": "2026-01-01T00:00:00Z",
    "messages": [
        {"role": "user", "text": "hi", "artifacts": []},
        {"role": "assistant", "text": "hello",
         "artifacts": [{"kind": "code", "language": "python", "content": "print(1)"}]},
    ],
}
_AISTUDIO_SCRAPE = {
    "url": "https://aistudio.google.com/prompts/abc",
    "title": "My prompt",
    "capturedAt": "2026-01-01T00:00:00Z",
    "messages": [
        {"role": "user", "text": "hi", "artifacts": []},
        {"role": "assistant", "text": "hello",
         "artifacts": [{"language": "python", "content": "print(1)"}]},
    ],
}
_PAGE_SCRAPE = {
    "url": "https://example.org/",
    "title": "Example",
    "description": "an example",
    "capturedAt": "2026-01-01T00:00:00Z",
    "text": "body text here",
    "links": [{"text": "Link", "href": "https://elsewhere.test/"}],
}


# ---------------------------------------------------------------------------
# Host helpers
# ---------------------------------------------------------------------------


def test_hostname_and_matching():
    assert web._hostname("https://claude.ai/new") == "claude.ai"
    assert web._hostname("about:blank") == ""  # the guest has no hierarchical URL
    assert web._hostname("https://a.claude.ai/x") == "a.claude.ai"
    assert web._host_matches("claude.ai", "claude.ai")
    assert web._host_matches("www.claude.ai", "claude.ai")  # subdomain
    assert web._host_matches("www.claude.ai", ".claude.ai")  # leading dot tolerated
    assert not web._host_matches("notclaude.ai", "claude.ai")
    assert not web._host_matches("", "claude.ai")
    # A trailing-dot FQDN dispatches like the bare host (the capture folder
    # already strips the dot), and a Unicode stem matches the punycode host.
    assert web._hostname("https://claude.ai./x") == "claude.ai"
    assert web._host_matches("xn--mnchen-3ya.de", "münchen.de")


def test_format_page_keeps_links_and_description_intact():
    md = web._format_page({
        "url": "https://e.org/",
        "title": "T",
        "description": "a\nb",
        "links": [{"text": "x]y", "href": "https://e.org/a_(b"}],
    })
    assert "- **Description:** a b" in md.splitlines()
    assert "- [x\\]y](https://e.org/a_%28b)" in md.splitlines()


# ---------------------------------------------------------------------------
# run(): settings → session, through the generator entry point
# ---------------------------------------------------------------------------


def _run_web(monkeypatch, *, config=None, prompt="https://example.com"):
    """Drive ``web.run()`` with the daemon stubbed out, returning
    ``(session kwargs, result)``. The stub stands in for ``_web_shared.daemon``
    (whose import is deferred into ``run()``), so this exercises the real
    settings → session wiring without a client."""
    import types

    captured: dict = {}

    async def _fake_run_web_session(**kwargs):
        captured.update(kwargs)

    pkg = types.ModuleType("_web_shared")
    pkg.__path__ = []  # type: ignore[attr-defined]
    mod = types.ModuleType("_web_shared.daemon")
    mod.run_web_session = _fake_run_web_session
    pkg.daemon = mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_web_shared", pkg)
    monkeypatch.setitem(sys.modules, "_web_shared.daemon", mod)

    res = run_generator_callable(
        web.run,
        prompt=prompt,
        config={"settings.set_yaml": config} if config else None,
    )
    assert res.ok, res.error
    return captured, res


def test_run_starts_a_session_with_the_profile(monkeypatch):
    kwargs, _res = _run_web(monkeypatch, config={"profile": "work"})
    # The whole call, key for key. There is one session shape and two
    # settings, so nothing about a second backend — mode, viewport,
    # chrome_path, xvfb, profile_dir — can quietly come back through here.
    assert set(kwargs) == {
        "url", "cell_name", "on_capture", "capture_button_label", "profile_name",
        "device",
    }
    assert kwargs["url"] == "https://example.com"
    assert kwargs["profile_name"] == "work"
    assert kwargs["cell_name"] == "Web"
    assert kwargs["capture_button_label"] == "Capture"
    assert callable(kwargs["on_capture"])
    assert kwargs["device"] == "desktop"
    # No settings at all → the profile falls back to "default".
    assert _run_web(monkeypatch)[0]["profile_name"] == "default"


def test_run_passes_the_device_setting_GN04(monkeypatch):
    """The `device` setting reaches the session; no setting is desktop."""
    kwargs, _res = _run_web(monkeypatch, config={"profile": "default", "device": "phone"})
    assert kwargs["device"] == "phone"
    assert _run_web(monkeypatch)[0]["device"] == "desktop"


def test_run_coerces_a_non_string_profile(monkeypatch):
    # An unquoted YAML value (`profile: 2024`) arrives as an int.
    assert _run_web(monkeypatch, config={"profile": 2024})[0]["profile_name"] == "2024"


def test_run_without_a_prompt_starts_a_session_anyway(monkeypatch):
    """The site is optional. A blank prompt is a session that opens on a blank
    page — not a failure — so nothing is written and the daemon still runs."""
    kwargs, res = _run_web(monkeypatch, prompt="")
    assert kwargs["url"] == ""
    assert res.files == {}  # no error.md: there is nothing wrong here
    assert kwargs["capture_button_label"] == "Capture"


# ---------------------------------------------------------------------------
# Capture-script selection (host → <pattern>.py, _default fallback)
# ---------------------------------------------------------------------------


def test_select_capture_script_by_host():
    scripts = {
        "claude.ai.py": "A",
        "aistudio.google.com.py": "B",
        "_default.py": "D",
        "README.md": "ignored",
        "nested/x.py": "ignored",
    }
    assert web._select_capture_script(scripts, "claude.ai") == ("claude.ai.py", "A")
    assert web._select_capture_script(scripts, "www.claude.ai") == ("claude.ai.py", "A")
    # no host-specific match → the _default fallback
    assert web._select_capture_script(scripts, "example.org") == ("_default.py", "D")


def test_select_capture_script_longest_wins_and_bytes():
    scripts = {
        "example.com.py": b"generic",
        "docs.example.com.py": b"specific",
    }
    # most specific stem wins; bytes decode to str
    assert web._select_capture_script(scripts, "docs.example.com") == (
        "docs.example.com.py", "specific")
    assert web._select_capture_script(scripts, "www.example.com") == (
        "example.com.py", "generic")


def test_select_capture_script_none_without_default():
    assert web._select_capture_script({"claude.ai.py": "A"}, "example.org") is None
    assert web._select_capture_script({}, "example.org") is None


# ---------------------------------------------------------------------------
# End-to-end capture dispatch (script vs built-in) through the bridge
# ---------------------------------------------------------------------------
#
# A capture writes into the RUNNING cell — one folder per press — so what a
# dispatch produced is read back off `res.files`, and no capture anywhere may
# create a cell.

_MARKER_SCRIPT = (
    "async def capture(ctx):\n"
    "    ctx.save('out.md', 'host=' + ctx.url)\n"
)




def test_dispatch_runs_matching_script():
    async def run():
        scripts = __import__("gen").config_folder("capture_scripts")
        dispatch = web.make_capture_dispatcher(scripts)
        await dispatch(FakeCtx("https://claude.ai/chat/x", scrape=_CLAUDE_SCRAPE))

    res = run_generator_callable(
        run,
        config_folders={"capture_scripts": {"claude.ai.py": _MARKER_SCRIPT}},
    )
    assert res.ok, res.error
    # the script ran, not the built-in — and into the capture's own folder
    assert res.files == {f"{_DIR}/out.md": "host=https://claude.ai/chat/x"}


def test_dispatch_default_script_fallback():
    async def run():
        scripts = __import__("gen").config_folder("capture_scripts")
        dispatch = web.make_capture_dispatcher(scripts)
        # host has no specific script → _default.py runs
        await dispatch(FakeCtx("https://anything.test/", scrape=_PAGE_SCRAPE))

    res = run_generator_callable(
        run,
        config_folders={"capture_scripts": {"_default.py": _MARKER_SCRIPT}},
    )
    assert res.ok, res.error
    assert res.files == {f"{_DIR}/out.md": "host=https://anything.test/"}


def test_dispatch_builtin_generic_page_when_no_scripts():
    async def run():
        scripts = __import__("gen").config_folder("capture_scripts")
        dispatch = web.make_capture_dispatcher(scripts)
        await dispatch(FakeCtx("https://example.org/", scrape=_PAGE_SCRAPE))

    res = run_generator_callable(run)  # no config_folders → built-in fallback
    assert res.ok, res.error
    assert set(res.files) == {
        f"{_DIR}/page.md", f"{_DIR}/page.html", f"{_DIR}/screenshot.png"}
    assert res.files[f"{_DIR}/page.md"].startswith("# Example")


def test_generic_capture_saves_screenshot_before_scrape_failure__F1992():
    class BrokenTextCtx(FakeCtx):
        async def evaluate(self, js):
            raise RuntimeError("page text became unavailable")

    ctx = BrokenTextCtx("https://example.org/")
    result = run_generator_callable(lambda: web._capture_page(ctx))
    assert not result.ok
    assert result.files[f"{_DIR}/screenshot.png"] == ctx._png


@pytest.mark.parametrize('host', ['claude.ai', 'aistudio.google.com'])
def test_chat_transcript_has_one_fence_for_each_separate_artifact__F2000(host):
    namespace = {}
    exec(_shipped_capture_scripts()[f'{host}.py'], namespace)
    scrape = {'truncated': True, 'messages': [{'role': 'assistant', 'text': 'Narrative.',
        'artifacts': [{'language': 'python', 'content': 'print(1)'}]}]}
    transcript = namespace['_transcript'](scrape)
    assert transcript.count('print(1)') == 1
    assert 'truncated' in transcript.lower()


def test_dispatch_generic_page_is_the_only_builtin():
    """The chat captures live in runner-staged scripts now, not in the
    generator: with no scripts staged, claude.ai gets the same generic page
    snapshot every other host gets (the accepted cost of the Browse runner)."""
    async def run():
        scripts = __import__("gen").config_folder("capture_scripts")
        dispatch = web.make_capture_dispatcher(scripts)
        await dispatch(FakeCtx("https://claude.ai/chat/x", scrape=_PAGE_SCRAPE))

    res = run_generator_callable(run)
    assert res.ok, res.error
    assert set(res.files) == {
        f"{_DIR}/page.md", f"{_DIR}/page.html", f"{_DIR}/screenshot.png"}


def test_dispatch_bad_script_is_contained():
    async def run():
        scripts = __import__("gen").config_folder("capture_scripts")
        dispatch = web.make_capture_dispatcher(scripts)
        # a matching script that defines no capture() — must not raise or emit
        await dispatch(FakeCtx("https://claude.ai/x", scrape=_CLAUDE_SCRAPE))

    res = run_generator_callable(
        run,
        config_folders={"capture_scripts": {"claude.ai.py": "x = 1  # no capture()\n"}},
    )
    assert res.ok, res.error
    # contained: nothing saved, and no built-in fallback behind it
    assert res.files == {}


def test_dispatch_script_exiting_at_load_is_contained():
    async def run():
        scripts = __import__("gen").config_folder("capture_scripts")
        dispatch = web.make_capture_dispatcher(scripts)
        await dispatch(FakeCtx("https://claude.ai/x", scrape=_CLAUDE_SCRAPE))

    res = run_generator_callable(
        run,
        config_folders={"capture_scripts": {"claude.ai.py": "import sys\nsys.exit(3)\n"}},
    )
    assert res.ok, res.error
    assert res.files == {}


def test_dispatch_failing_capture_surfaces_as_an_ordinary_error():
    """A SystemExit inside capture() must not escape as a BaseException (it
    would end the whole session): it is logged with the script name and
    re-raised as a RuntimeError the daemon's per-press handler catches."""
    async def run():
        scripts = __import__("gen").config_folder("capture_scripts")
        dispatch = web.make_capture_dispatcher(scripts)
        with pytest.raises(RuntimeError):
            await dispatch(FakeCtx("https://claude.ai/x", scrape=_CLAUDE_SCRAPE))

    res = run_generator_callable(
        run,
        config_folders={"capture_scripts": {
            "claude.ai.py": "async def capture(ctx):\n    raise SystemExit(2)\n"}},
    )
    assert res.ok, res.error
    assert any("'claude.ai.py'" in line and "failed:" in line for line in res.logs)


# ---------------------------------------------------------------------------
# The capture scripts the runner actually ships
# ---------------------------------------------------------------------------
#
# The `capture-chat` runner's folder is the whole chat-capture story now —
# nothing in the generator knows what a conversation is. These two run the real
# files off disk, staged exactly as the host stages a runner folder, and dispatched by
# host through the real dispatcher (so they also pin that each script is picked
# for its own site and not the other's).


def _shipped_capture_scripts() -> dict[str, str]:
    folder = os.path.join(_RUNNER_DIR, "files", "capture_scripts")
    out: dict[str, str] = {}
    for name in sorted(os.listdir(folder)):
        if name.endswith(".py"):
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                out[name] = fh.read()
    assert out, "the capture-chat runner ships no capture scripts"
    return out


def test_the_shipped_claude_script_captures_a_conversation():
    async def run():
        scripts = __import__("gen").config_folder("capture_scripts")
        dispatch = web.make_capture_dispatcher(scripts)
        await dispatch(FakeCtx("https://claude.ai/chat/x", scrape=_CLAUDE_SCRAPE))

    res = run_generator_callable(
        run, config_folders={"capture_scripts": _shipped_capture_scripts()})
    assert res.ok, res.error
    body = res.files[f"{_DIR}/conversation.md"]
    assert body.startswith("# Claude.ai conversation")
    assert "https://claude.ai/chat/abc" in body
    assert "## 1. User" in body and "## 2. Assistant" in body
    assert "```python" in body and "print(1)" in body
    # each fenced code block also lands as its own file, extension by language
    assert res.files[f"{_DIR}/artifact_1.py"] == "print(1)"
    assert json.loads(res.files[f"{_DIR}/messages.json"])["title"] == "My chat"


def test_the_shipped_aistudio_script_captures_a_conversation():
    async def run():
        scripts = __import__("gen").config_folder("capture_scripts")
        dispatch = web.make_capture_dispatcher(scripts)
        await dispatch(FakeCtx("https://aistudio.google.com/prompts/x",
                               scrape=_AISTUDIO_SCRAPE))

    res = run_generator_callable(
        run, config_folders={"capture_scripts": _shipped_capture_scripts()})
    assert res.ok, res.error
    body = res.files[f"{_DIR}/conversation.md"]
    assert body.startswith("# Google AI Studio conversation")
    assert "https://aistudio.google.com/prompts/abc" in body
    assert "## 1. User" in body and "## 2. Assistant" in body
    assert "```python" in body and "print(1)" in body
    assert res.files[f"{_DIR}/artifact_1.py"] == "print(1)"
    assert json.loads(res.files[f"{_DIR}/messages.json"])["title"] == "My prompt"


def test_the_shipped_aistudio_script_keeps_only_the_first_token_of_a_language():
    # A code-block header's innerText can carry icon text after the language;
    # it must neither leak into the fence info string nor demote the extension.
    scrape = json.loads(json.dumps(_AISTUDIO_SCRAPE))
    scrape["messages"][1]["artifacts"][0]["language"] = "Python\n\ncontent_copy"

    async def run():
        scripts = __import__("gen").config_folder("capture_scripts")
        dispatch = web.make_capture_dispatcher(scripts)
        await dispatch(FakeCtx("https://aistudio.google.com/prompts/x",
                               scrape=scrape))

    res = run_generator_callable(
        run, config_folders={"capture_scripts": _shipped_capture_scripts()})
    assert res.ok, res.error
    body = res.files[f"{_DIR}/conversation.md"]
    assert "```python\nprint(1)\n```" in body
    assert "content_copy" not in body
    assert res.files[f"{_DIR}/artifact_1.py"] == "print(1)"


# ---------------------------------------------------------------------------
# The claude.ai scrape itself, run under Node against a synthetic page
# ---------------------------------------------------------------------------
#
# `_SCRAPE` is page JS; the tests above hand the script a canned result. These
# run the real function (read off the config folder, the canonical copy)
# against a fake DOM that implements exactly the selector forms and element
# members the scrape uses — no browser, no network, no claude.ai.

_CLAUDE_SCRIPT = os.path.join(
    _WEB_DIR, "configs", "web--capture-chat", "files", "capture_scripts", "claude.ai.py")

_FAKE_DOM = r"""
const fs = require('fs');
const { page, scrape } = JSON.parse(fs.readFileSync(0, 'utf8'));
const errors = [];
function parseCompound(s) {
  const c = { tag: null, cls: [], attrs: [] };
  const head = /^[a-z][a-z0-9-]*/.exec(s);
  let i = head ? head[0].length : 0;
  if (head) c.tag = head[0];
  const re = /\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/y;
  while (i < s.length) {
    re.lastIndex = i;
    const m = re.exec(s);
    if (!m) { errors.push(s); throw new Error('fake DOM: unsupported selector ' + s); }
    if (m[1] !== undefined) c.cls.push(m[1]); else c.attrs.push([m[2], m[3]]);
    i = re.lastIndex;
  }
  return c;
}
function matchCompound(el, c) {
  return (!c.tag || el.tag === c.tag)
    && c.cls.every((k) => el.cls.includes(k))
    && c.attrs.every(([k, v]) => k in el.attrs && (v === undefined || el.attrs[k] === v));
}
function matchSelector(el, sel) {
  const parts = sel.trim().split(/\s+/).map(parseCompound);
  if (!matchCompound(el, parts[parts.length - 1])) return false;
  let j = parts.length - 2;
  for (let a = el.parentElement; j >= 0 && a; a = a.parentElement) {
    if (matchCompound(a, parts[j])) j--;
  }
  return j < 0;
}
class El {
  constructor(n, parent) {
    this.tag = n.tag || 'div';
    this.attrs = n.attrs || {};
    this.cls = n.cls || [];
    this.text = n.text || '';
    this.parentElement = parent;
    this.children = (n.children || []).map((c) => new El(c, this));
  }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  get classList() { return { contains: (k) => this.cls.includes(k) }; }
  get className() { return this.cls.join(' '); }
  get innerText() {
    return [this.text, ...this.children.map((c) => c.innerText)].filter(Boolean).join('\n');
  }
  matches(sel) { return sel.split(',').some((s) => matchSelector(this, s)); }
  closest(sel) {
    for (let e = this; e; e = e.parentElement) if (e.matches(sel)) return e;
    return null;
  }
  *walk() { for (const c of this.children) { yield c; yield* c.walk(); } }
  querySelectorAll(sel) { return [...this.walk()].filter((e) => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const body = new El({ tag: 'body', children: page }, null);
globalThis.document = {
  title: 'My chat',
  querySelectorAll: (s) => body.querySelectorAll(s),
  querySelector: (s) => body.querySelector(s),
};
globalThis.location = { href: 'https://claude.ai/chat/abc' };
const result = (new Function('return (' + scrape + ')'))()();
process.stdout.write(JSON.stringify({ result, errors }));
"""


def _el(tag="div", text="", *, cls=(), attrs=None, children=()):
    return {"tag": tag, "text": text, "cls": list(cls),
            "attrs": dict(attrs or {}), "children": list(children)}


def _claude_scrape_js() -> str:
    import ast

    with open(_CLAUDE_SCRIPT, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_SCRAPE" for t in node.targets):
            return node.value.value
    raise AssertionError("claude.ai.py defines no _SCRAPE")


def _run_claude_scrape(page: list) -> dict:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node is required to run the claude.ai scrape")
    proc = subprocess.run(
        ["node", "-e", _FAKE_DOM],
        input=json.dumps({"page": page, "scrape": _claude_scrape_js()}),
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["errors"] == [], out["errors"]   # the fake DOM parsed every selector
    return out["result"]


def _user_turn(text):
    # claude.ai's user bubble: both labelled markers on the message body
    return _el(attrs={"data-test-render-count": "1"}, children=[
        _el(text=text, cls=["font-user-message"], attrs={"data-testid": "user-message"}),
        _el("button", "Edit"),
    ])


def _assistant_turn(text, *, cls="font-claude-message", testid=None, inner_count=False):
    body = _el(text=text, cls=[cls], attrs={"data-testid": testid} if testid else None)
    if inner_count:   # a render-count nested inside the turn's own
        body = _el(attrs={"data-test-render-count": "2"}, children=[body])
    return _el(attrs={"data-test-render-count": "1"}, children=[body, _el("button", "Copy")])


def _roles_and_texts(result):
    return [(m["role"], m["text"]) for m in result["messages"]]


def test_claude_scrape_keeps_roles_when_one_labelled_marker_goes_stale__F1999():
    """F1999: the assistant markers of both labelled strategies are gone
    (claude.ai renamed its class), so each labelled strategy sees only the user
    turns and the one-per-turn ``[data-test-render-count]`` strategy has the
    most matches. Its turns are labelled by the markers they contain — the user
    ones by the live user marker, the rest as the assistant — instead of every
    message coming back 'unknown'."""
    page = [_user_turn("hi"), _assistant_turn("hello", cls="font-claude-response"),
            _user_turn("again"), _assistant_turn("sure", cls="font-claude-response")]
    result = _run_claude_scrape(page)
    assert [m["role"] for m in result["messages"]] == [
        "user", "assistant", "user", "assistant"]
    assert "hello" in result["messages"][1]["text"]


def test_claude_scrape_counts_a_nested_render_count_once__F1999():
    """F1999: a ``[data-test-render-count]`` nested inside a turn's own is the
    same turn, not another message — with the labelled markers intact, the
    render-count strategy must not out-count them with duplicates."""
    page = [_user_turn("hi"), _assistant_turn("hello", inner_count=True),
            _user_turn("again"), _assistant_turn("sure", inner_count=True)]
    result = _run_claude_scrape(page)
    assert _roles_and_texts(result) == [
        ("user", "hi"), ("assistant", "hello"), ("user", "again"), ("assistant", "sure")]


def test_claude_scrape_prefers_labelled_markers_on_a_full_page__F1999():
    """F1999: with every marker live the labelled strategies win the tie, so
    each message is its body alone, without the turn's button text."""
    page = [_user_turn("hi"), _assistant_turn("hello", testid="assistant-message")]
    result = _run_claude_scrape(page)
    assert _roles_and_texts(result) == [("user", "hi"), ("assistant", "hello")]


def test_claude_scrape_prefers_labelled_roles_over_unknown_turns__F1999():
    """F1999: when the render-count elements hold no message marker, their
    'unknown' turns lose to a strategy that labelled roles."""
    page = [
        _el(text="hi", cls=["font-user-message"]),
        _el(text="hello", cls=["font-claude-message"]),
        _el(attrs={"data-test-render-count": "1"}, text="banner"),
        _el(attrs={"data-test-render-count": "1"}, text="footer"),
        _el(attrs={"data-test-render-count": "1"}, text="toast"),
    ]
    result = _run_claude_scrape(page)
    assert _roles_and_texts(result) == [("user", "hi"), ("assistant", "hello")]


def test_claude_scrape_keeps_every_turn_when_no_marker_is_live__F1999():
    """F1999: with every labelled marker stale the render-count turns still
    come back, each once, as 'unknown' — no role is known, but no turn is
    dropped."""
    page = [_assistant_turn("hi", cls="x"), _assistant_turn("hello", cls="y")]
    result = _run_claude_scrape(page)
    assert _roles_and_texts(result) == [
        ("unknown", "hi\nCopy"), ("unknown", "hello\nCopy")]


def _render_count_wrapper(*turns):
    return _el(attrs={"data-test-render-count": "0"}, children=list(turns))


@pytest.mark.parametrize("turns", [
    [("user", "hi"), ("assistant", "hello"), ("user", "again"), ("assistant", "sure")],
    [("user", "hi"), ("assistant", "hello")],
])
def test_claude_scrape_descends_into_a_render_count_that_wraps_turns__F1999(turns):
    """F1999: a ``[data-test-render-count]`` on a container around the turns is
    not itself a turn. With the assistant markers stale, keeping only the
    outermost render-count would collapse the page into one 'user' element
    and the user-only labelled strategy would win, dropping every reply. The
    wrapper is skipped instead and each turn inside it kept once."""
    page = [_render_count_wrapper(*(
        _user_turn(t) if role == "user" else _assistant_turn(t, cls="font-claude-response")
        for role, t in turns))]
    result = _run_claude_scrape(page)
    assert _roles_and_texts(result) == [
        (role, f"{t}\n{'Edit' if role == 'user' else 'Copy'}") for role, t in turns]


def test_claude_scrape_handles_render_counts_nested_both_ways__F1999():
    """F1999: a wrapper render-count around the turns and a render-count inside
    a turn on one page — the wrapper is skipped, the inner one is part of its
    turn, and each turn comes back once with its role."""
    page = [_render_count_wrapper(
        _user_turn("hi"),
        _assistant_turn("hello", cls="font-claude-response", inner_count=True),
        _user_turn("again"),
        _assistant_turn("sure", cls="font-claude-response", inner_count=True),
    )]
    result = _run_claude_scrape(page)
    assert _roles_and_texts(result) == [
        ("user", "hi\nEdit"), ("assistant", "hello\nCopy"),
        ("user", "again\nEdit"), ("assistant", "sure\nCopy")]


# ---------------------------------------------------------------------------
# The control-WS RPC, against an in-process fake client
# ---------------------------------------------------------------------------
#
# Nothing here talks to a real Electron client; the fake below IS the client. It
# stands in for the renderer's socket: it records every `kind:"rpc"` frame the
# daemon sends and answers it by feeding the reply back through the daemon's
# real up-message router, so the request ids, the pending-future bookkeeping,
# the wire shapes and the ctx accessors are all exercised end to end.


class FakeClientWS:
    """A control-WS client socket serving one tab, answering RPCs from a
    canned table.

    ``answers`` maps a method to what the client would return; a value wrapped
    in :class:`Err` comes back as an ``rpc_error``, and a method that is absent
    is simply never answered (the timeout path)."""

    class Err(str):
        pass

    def __init__(self, tabs, router, answers=None, *, tab="tab-1"):
        self.tabs = tabs
        self.router = router
        self.tab = tab
        self.answers = dict(answers or {})
        self.sent: list[dict] = []
        self.closed = False

    async def send_str(self, data: str) -> None:
        msg = json.loads(data)
        self.sent.append(msg)
        if msg.get("kind") != "rpc" or msg["method"] not in self.answers:
            return  # nobody home for this one → the caller times out
        value = self.answers[msg["method"]]
        if callable(value):
            value = value(msg.get("params") or {})
        reply = (
            {"kind": "rpc_error", "id": msg["id"], "message": str(value)}
            if isinstance(value, FakeClientWS.Err)
            else {"kind": "rpc_result", "id": msg["id"], "value": value}
        )
        await self.router(self.tabs, self, reply)

    async def hello(self, caps=None, client="electron", tab=None):
        await self.router(self.tabs, self, {
            "kind": "hello",
            "tab": tab or self.tab,
            "client": client,
            # camelCase on the wire — straight off the shell's contextBridge.
            "caps": caps if caps is not None else {
                "nativeGuests": True, "guestCapture": True,
            },
        })

    async def navigated(self, url: str) -> None:
        await self.router(self.tabs, self, {"kind": "url", "url": url})


_PNG = b"\x89PNG\r\n\x1a\n-fake-"
_FOLDER = "_runs/web-0123abcd"


def _tabs_with(daemon, protocol, *ids):
    """A daemon tab registry holding the tabs ``ids`` (``tab-<n>``)."""
    tabs = daemon._Tabs()
    for tab_id in ids:
        tabs.tabs[tab_id] = protocol._TabState(
            tab=tab_id, path=f"{_FOLDER}/{tab_id}.webview")
    return tabs


def _attached_client(answers=None):
    """One tab (``tab-1``) with a fake client socket ready to attach; returns
    the tab's state, the socket and the modules."""
    protocol, capture, daemon = _shared()
    tabs = _tabs_with(daemon, protocol, "tab-1")
    ws = FakeClientWS(tabs, daemon._handle_client_message, answers)
    return tabs.tabs["tab-1"], ws, (protocol, capture, daemon)


def _ctx(state, capture_mod, *, url="https://example.com/", timeout=60.0,
         capture_dir=_DIR):
    return capture_mod.CaptureContext(
        url=url, capture_count=1, state=state, capture_dir=capture_dir,
        timeout=timeout)


def test_native_hello_registers_the_client_and_its_caps(native_bridge):
    state, ws, (_p, _c, _d) = _attached_client()
    asyncio.run(ws.hello())
    assert state.client is ws
    assert state.client_kind == "electron"
    # camelCase in, snake_case out — the shape gen.client_caps() reports
    assert state.client_caps == {"native_guests": True, "guest_capture": True}


def test_native_hello_for_an_unknown_tab_is_ignored(native_bridge):
    """A socket serves the tab its hello names — and only a tab this run
    has. Before a hello, nothing the socket says reaches any tab."""
    state, ws, _mods = _attached_client()
    asyncio.run(ws.navigated("https://example.com/before-hello"))
    assert state.current_url == ""
    asyncio.run(ws.hello(tab="tab-9"))
    assert state.client is None
    assert ws not in ws.tabs.served


def test_native_each_tab_has_its_own_client_and_url(native_bridge):
    """Two tabs, two sockets: each hello attaches to its own tab, and a `url`
    message moves only its own tab."""
    protocol, _capture, daemon = _shared()
    tabs = _tabs_with(daemon, protocol, "tab-1", "tab-2")
    one = FakeClientWS(tabs, daemon._handle_client_message, tab="tab-1")
    two = FakeClientWS(tabs, daemon._handle_client_message, tab="tab-2")

    async def drive():
        await one.hello()
        await two.hello()
        await two.navigated("https://two.example/")
        await one.navigated("https://one.example/")

    asyncio.run(drive())
    assert tabs.tabs["tab-1"].client is one and tabs.tabs["tab-2"].client is two
    assert tabs.tabs["tab-1"].current_url == "https://one.example/"
    assert tabs.tabs["tab-2"].current_url == "https://two.example/"


def test_native_url_messages_feed_the_session_url(native_bridge):
    state, ws, _mods = _attached_client()
    asyncio.run(ws.hello())
    asyncio.run(ws.navigated("https://example.com/deep/page"))
    assert state.current_url == "https://example.com/deep/page"
    asyncio.run(ws.navigated("   "))  # blanks are ignored, not applied
    assert state.current_url == "https://example.com/deep/page"


def test_native_rpc_round_trips(native_bridge):
    state, ws, (protocol, capture, _d) = _attached_client({
        "evaluate": {"title": "Example"},
        "html": "<html>hi</html>",
        "screenshot": base64.b64encode(_PNG).decode("ascii"),
        "url": "https://example.com/now",
        "navigate": None,
    })

    async def drive():
        await ws.hello()
        ctx = _ctx(state, capture)
        assert await ctx.evaluate("() => ({title: document.title})") == {"title": "Example"}
        assert await ctx.html() == "<html>hi</html>"
        assert await ctx.screenshot_png() == _PNG
        assert await ctx.current_url() == "https://example.com/now"
        await ctx.navigate("https://example.com/next")

    asyncio.run(drive())
    rpcs = [m for m in ws.sent if m.get("kind") == "rpc"]
    assert [m["method"] for m in rpcs] == [
        "evaluate", "html", "screenshot", "url", "navigate"]
    # ids are generated per session and unique
    assert len({m["id"] for m in rpcs}) == len(rpcs)
    # THE calling convention, in one place: a capture hands over a zero-arg
    # function source and the wire carries the wrapped invocation.
    assert rpcs[0]["params"] == {"js": "(() => ({title: document.title}))()"}
    assert rpcs[0]["params"]["js"] == protocol._evaluate_expression(
        "() => ({title: document.title})")
    assert rpcs[1]["params"] == {} and rpcs[2]["params"] == {}
    assert rpcs[4]["params"] == {"url": "https://example.com/next"}
    # every future was cleaned up
    assert state.rpc_pending == {}


def test_native_rpc_error_surfaces_the_client_message(native_bridge):
    state, ws, (_p, capture, _d) = _attached_client(
        {"evaluate": FakeClientWS.Err("collimate-guest/guest-gone: it's gone")})

    async def drive():
        await ws.hello()
        with pytest.raises(capture.CaptureError) as exc:
            await _ctx(state, capture).evaluate("() => 1")
        assert "guest-gone" in str(exc.value)

    asyncio.run(drive())
    assert state.rpc_pending == {}


def test_native_rpc_times_out_without_an_answer(native_bridge):
    state, ws, (_p, capture, _d) = _attached_client({})  # answers nothing

    async def drive():
        await ws.hello()
        with pytest.raises(capture.CaptureError) as exc:
            await _ctx(state, capture, timeout=0.05).html()
        assert "did not answer" in str(exc.value)

    asyncio.run(drive())
    assert state.rpc_pending == {}  # the timed-out future is dropped, not leaked


def test_native_capture_unavailable_without_a_client():
    state, _ws, (_p, capture, _d) = _attached_client()  # no hello

    async def drive():
        with pytest.raises(capture.CaptureUnavailable) as exc:
            await _ctx(state, capture).html()
        assert "desktop app" in str(exc.value)

    asyncio.run(drive())
    # CaptureUnavailable is a CaptureError, so one except clause catches both
    assert issubclass(capture.CaptureUnavailable, capture.CaptureError)


def test_native_capture_unavailable_without_the_capability(native_bridge):
    state, ws, (_p, capture, _d) = _attached_client({"html": "<html></html>"})

    async def drive():
        await ws.hello(caps={"nativeGuests": True, "guestCapture": False})
        with pytest.raises(capture.CaptureUnavailable) as exc:
            await _ctx(state, capture).html()
        assert "guestCapture" in str(exc.value)

    asyncio.run(drive())
    assert not [m for m in ws.sent if m.get("kind") == "rpc"]  # refused before sending


def test_native_disconnect_fails_pending_calls(native_bridge):
    state, ws, (_p, capture, daemon) = _attached_client({})  # never answers

    async def drive():
        await ws.hello()
        call = asyncio.ensure_future(_ctx(state, capture).evaluate("() => 1"))
        await asyncio.sleep(0)  # let it register its pending future
        assert state.rpc_pending
        daemon._detach_client(state)  # what the WS handler does on disconnect
        with pytest.raises(capture.CaptureUnavailable):
            await call

    asyncio.run(drive())
    assert state.rpc_pending == {} and state.client is None


def test_native_capture_scripts_work_unchanged_over_the_rpc():
    """The contract bar: a runner-staged capture script is handed a
    CaptureContext backed by the control-WS RPC and saves exactly the files it
    saves against any other context — a capture script never learns how its
    `ctx` reaches the page."""
    state, ws, (_p, capture, _d) = _attached_client({
        "evaluate": _CLAUDE_SCRAPE,
        "html": "<html>doc</html>",
        "screenshot": base64.b64encode(_PNG).decode("ascii"),
    })

    async def run():
        await ws.hello()
        dispatch = web.make_capture_dispatcher(
            __import__("gen").config_folder("capture_scripts"))
        await dispatch(_ctx(state, capture, url="https://claude.ai/chat/x"))

    res = run_generator_callable(
        run, config_folders={"capture_scripts": _shipped_capture_scripts()})
    assert res.ok, res.error
    assert res.files[f"{_DIR}/conversation.md"].startswith("# Claude.ai conversation")
    assert res.files[f"{_DIR}/artifact_1.py"] == "print(1)"


def test_native_generic_page_capture_over_the_rpc():
    state, ws, (_p, capture, _d) = _attached_client({
        "evaluate": lambda params: ({"html": "<html>doc</html>", "truncated": False}
            if 'outerHTML' in params['js'] else _PAGE_SCRAPE),
        "screenshot": base64.b64encode(_PNG).decode("ascii"),
    })

    async def run():
        await ws.hello()
        dispatch = web.make_capture_dispatcher({})
        await dispatch(_ctx(state, capture, url="https://example.org/"))

    res = run_generator_callable(run)
    assert res.ok, res.error
    # Bounded HTML evaluation and screenshot came back over the RPC.
    assert res.files[f"{_DIR}/page.html"] == "<html>doc</html>"
    assert res.files[f"{_DIR}/screenshot.png"] == _PNG
    assert [message['method'] for message in ws.sent if message.get('kind') == 'rpc'] == [
        'screenshot', 'evaluate', 'evaluate']


def test_capture_context_save_stages_into_the_capture_folder():
    """`ctx.save` is the whole write side of the contract: relative to THIS
    capture's folder, str or bytes, nested paths allowed, and remembered in
    save order (which is what the closing pin reads)."""
    state, _ws, (_p, capture, _d) = _attached_client()

    async def run():
        ctx = _ctx(state, capture, capture_dir="example.org-20260101-000000")
        ctx.save("page.md", "# page\n")
        ctx.save("/screenshot.png", b"\x89PNG")  # a leading slash is not an escape
        ctx.save("images/hero.png", b"\x89PNG-2")
        assert ctx.saved == [
            f"{_DIR}/page.md", f"{_DIR}/screenshot.png", f"{_DIR}/images/hero.png"]

    res = run_generator_callable(run)
    assert res.ok, res.error
    assert res.files == {
        f"{_DIR}/page.md": "# page\n",
        f"{_DIR}/screenshot.png": b"\x89PNG",
        f"{_DIR}/images/hero.png": b"\x89PNG-2",
    }


def test_capture_context_save_refuses_to_leave_the_capture_folder():
    """The containment promise is enforced, not trusted: a capture script is
    runner-supplied code, so a `relpath` that would climb out of the press's own
    folder — or name nothing at all — is refused rather than written."""
    state, _ws, (_p, capture, _d) = _attached_client()

    async def run():
        ctx = _ctx(state, capture)
        for bad in ("../escape.md", "images/../../escape.md", "..\\escape.md",
                    "/../escape.md", "..", "", "   ", "/", "./"):
            with pytest.raises(ValueError) as exc:
                ctx.save(bad, "must not be written")
            assert "capture" in str(exc.value)
        assert ctx.saved == []  # nothing staged on the way through
        # The legitimate shapes still work, normalised on the way in.
        ctx.save("/page.md", "# page\n")
        ctx.save("./images/hero.png", b"\x89PNG")
        ctx.save("nested\\deep.txt", "backslashes are separators, not escapes")

    res = run_generator_callable(run)
    assert res.ok, res.error
    assert set(res.files) == {
        f"{_DIR}/page.md", f"{_DIR}/images/hero.png", f"{_DIR}/nested/deep.txt"}


def test_native_control_ws_end_to_end(native_bridge):
    """The one test that goes through a real socket: boot the daemon's server,
    attach as a client over the actual control WebSocket, and complete an
    evaluate round-trip. Covers the handler, the JSON framing and the route
    table (which must be the control WS and nothing else)."""
    aiohttp = pytest.importorskip("aiohttp")
    protocol, capture, daemon = _shared()

    async def recv(ws):
        return json.loads(await asyncio.wait_for(ws.receive_str(), 5))

    async def settle(pred):
        for _ in range(200):
            if pred():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition never became true")

    async def drive():
        tabs = _tabs_with(daemon, protocol, "tab-1", "tab-2")
        state = tabs.tabs["tab-2"]
        sock = daemon._bind_free_port(9700)
        port = sock.getsockname()[1]
        runner = await daemon._start_aiohttp(sock, tabs)
        try:
            async with aiohttp.ClientSession() as http:
                base = f"http://127.0.0.1:{port}"
                # No reverse proxy, no service worker, no index — the session
                # server has exactly one endpoint, shared by every tab.
                async with http.get(base + "/") as resp:
                    assert resp.status == 404
                async with http.ws_connect(base + "/__collimate/ws") as ws:
                    await ws.send_json({
                        "kind": "hello", "tab": "tab-2", "client": "electron",
                        "caps": {"nativeGuests": True, "guestCapture": True},
                    })
                    await settle(lambda: state.client is not None)
                    assert tabs.tabs["tab-1"].client is None

                    ctx = capture.CaptureContext(
                        url="https://example.com/", capture_count=1,
                        state=state, timeout=5.0)
                    call = asyncio.ensure_future(ctx.evaluate("() => document.title"))
                    req = await recv(ws)
                    assert req["kind"] == "rpc" and req["method"] == "evaluate"
                    assert req["params"] == {"js": "(() => document.title)()"}
                    await ws.send_json(
                        {"kind": "rpc_result", "id": req["id"], "value": "Example"})
                    assert await asyncio.wait_for(call, 5) == "Example"

                    await ws.send_json({"kind": "url", "url": "https://example.com/x"})
                    await settle(lambda: state.current_url == "https://example.com/x")
                # Closing the socket detaches the client for real.
                await settle(lambda: state.client is None)
        finally:
            await runner.cleanup()

    asyncio.run(drive())


def test_native_control_ws_carries_a_reply_over_4_mib(native_bridge):
    """A capture reply is one frame; aiohttp's 4 MiB default would close the
    socket on a large page's html() and fail the call mid-capture."""
    aiohttp = pytest.importorskip("aiohttp")
    protocol, capture, daemon = _shared()

    async def drive():
        tabs = _tabs_with(daemon, protocol, "tab-1")
        state = tabs.tabs["tab-1"]
        sock = daemon._bind_free_port(9700)
        port = sock.getsockname()[1]
        runner = await daemon._start_aiohttp(sock, tabs)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(
                        f"http://127.0.0.1:{port}/__collimate/ws") as ws:
                    await ws.send_json({
                        "kind": "hello", "tab": "tab-1", "client": "electron",
                        "caps": {"nativeGuests": True, "guestCapture": True},
                    })
                    for _ in range(200):
                        if state.client is not None:
                            break
                        await asyncio.sleep(0.01)
                    assert state.client is not None
                    ctx = capture.CaptureContext(
                        url="https://example.com/", capture_count=1,
                        state=state, timeout=5.0)
                    call = asyncio.ensure_future(ctx.html())
                    req = json.loads(await asyncio.wait_for(ws.receive_str(), 5))
                    big = "x" * (5 * 1024 * 1024)
                    await ws.send_json(
                        {"kind": "rpc_result", "id": req["id"], "value": big})
                    assert await asyncio.wait_for(call, 5) == big
                    assert state.client is not None
        finally:
            await runner.cleanup()

    asyncio.run(drive())


def test_bind_free_port_keeps_the_port_bound():
    """The port is held from the scan to the listen: a second scan from the
    same start moves past it instead of handing out the same number."""
    _p, _c, daemon = _shared()
    first = daemon._bind_free_port(9700)
    try:
        second = daemon._bind_free_port(first.getsockname()[1])
        try:
            assert second.getsockname()[1] != first.getsockname()[1]
        finally:
            second.close()
    finally:
        first.close()


def test_the_cdp_stack_is_gone():
    """One backend, and the proof it stayed one: no `cdp/` package, no second
    capture context, and a package surface of exactly four names."""
    import types

    _protocol, capture, _daemon = _shared()
    assert not os.path.exists(
        os.path.join(GENERATORS, "_web_shared", "cdp"))
    assert not hasattr(capture, "CdpCaptureContext")
    with pytest.raises(ImportError):
        importlib.import_module("_web_shared.cdp")

    import _web_shared

    exported = {n for n, v in vars(_web_shared).items()
                if not n.startswith("_") and not isinstance(v, types.ModuleType)}
    assert exported == {
        "CaptureContext", "CaptureError", "CaptureUnavailable", "run_web_session"}


# ---------------------------------------------------------------------------
# Where a capture lands — one folder per press, in the running cell
# ---------------------------------------------------------------------------


def test_capture_dir_name_is_the_host_and_a_timestamp():
    _p, _c, daemon = _shared()
    name = daemon._capture_dir_name("https://docs.example.com:8443/a/b?q=1")
    # host only: the port is dropped, dots and hyphens survive, and the
    # timestamp is local wall-clock to the second
    stamp = name.split("docs.example.com-", 1)[1]
    assert name.startswith("docs.example.com-")
    assert re.fullmatch(r"\d{8}-\d{6}", stamp), name
    assert daemon._capture_dir_name("HTTPS://Example.ORG/").startswith("example.org-")
    # nothing loaded yet (or a non-http page) → the neutral fallback
    for blank in ("", "about:blank"):
        assert daemon._capture_dir_name(blank).startswith("page-")


def test_capture_dir_names_are_filesystem_safe():
    _p, _c, daemon = _shared()
    # A host is already restricted, but nothing may reach the path layer that
    # could climb out of the cell.
    for url in ("https://a_b.example.com/", "https://[::1]:9000/", "https://x/"):
        name = daemon._capture_dir_name(url)
        assert "/" not in name and ".." not in name
        assert re.fullmatch(r"[a-z0-9.-]+-\d{8}-\d{6}", name), name


def test_claimed_capture_dirs_are_unique_within_the_session(monkeypatch):
    """The timestamp is only one second wide, so the name a press actually
    writes under is claimed against the ones the session already took —
    otherwise two presses inside one second share a folder and the second
    quietly overwrites the first."""
    _p, _c, daemon = _shared()
    monkeypatch.setattr(daemon.time, "strftime", lambda fmt: "20260101-000001")
    used: set[str] = set()
    assert daemon._claim_capture_dir("https://example.org/a", used) == (
        "example.org-20260101-000001")
    # same host, same second → the readable base survives, with a suffix
    assert daemon._claim_capture_dir("https://example.org/b", used) == (
        "example.org-20260101-000001-2")
    assert daemon._claim_capture_dir("https://example.org/c", used) == (
        "example.org-20260101-000001-3")
    # a different host in that same second never collided in the first place
    assert daemon._claim_capture_dir("https://other.test/", used) == (
        "other.test-20260101-000001")
    assert len(used) == 4
    # …and the naming itself stays pure: it is the caller's set that remembers
    assert daemon._capture_dir_name("https://example.org/a") == (
        "example.org-20260101-000001")


# ---------------------------------------------------------------------------
# The session lifecycle — one run loop, its tabs, and what Stop leaves behind
# ---------------------------------------------------------------------------
#
# `run_web_session` really runs here: its aiohttp server binds a loopback port
# and the loop turns. The clicks are stubbed at the one place the daemon reads
# them — `poll_renderer_action_event` — so a test scripts the presses on the
# tabs' toolbars and then the Stop that ends the run.

_LABEL = "web-0123abcd"
_TAB = f"{_FOLDER}/tab-1.webview"


def _toolbar(action, file, payload=None):
    """A press on a toolbar button of the file ``file`` (a tab's, for a real
    press): the host stamps the action with that file as its source."""
    event = {"action_id": action, "source": file}
    if payload is not None:
        event["payload"] = payload
    return event


def _scripted_renderer(bridge_hook, presses):
    """Feed the run loop ``presses`` (action events), then hit Stop.

    The harness stops a daemon as soon as it serves a port, which would end
    the session before its first turn; these tests drive the loop themselves,
    so that shortcut is off and Stop arrives from the script instead."""
    def setup(bridge):
        bridge._autocancel_on_serve = False
        queued = list(presses)

        def poll(*, source="", action_id=""):
            if queued:
                return queued.pop(0)
            bridge._signal_stop()  # the Stop button
            return None

        bridge.poll_renderer_action_event = poll

    bridge_hook["setup"] = setup


async def _save_a_page(ctx):
    ctx.save("page.md", f"# {ctx.url}\n")
    ctx.save("screenshot.png", b"\x89PNG")


def _session_run(url="https://example.com/x", on_capture=_save_a_page, **session):
    _p, _c, daemon = _shared()

    async def run():
        await daemon.run_web_session(
            url=url,
            cell_name="Web",
            on_capture=on_capture,
            capture_button_label="Capture",
            **session,
        )

    return run


def _run_session(run, **kw):
    """Run a session as a hosted web run would: its label and folder, and the
    manifest's one UI file type."""
    kw.setdefault("ui_files", [".webview"])
    return run_generator_callable(
        run, label=_LABEL, channel="chan", run_id="run-1", timeout=30, **kw)


def _tab_writes(res):
    """``{path: artifact}`` for every tab file the run flushed, in order."""
    out: dict = {}
    for update in _RESULT_UPDATES[id(res)]:
        for path, content in update["files"].items():
            if path.endswith(".webview"):
                out[path] = json.loads(content)
    return out


def test_the_first_tab_is_written_revealed_and_deleted_at_stop(bridge_hook):
    """The run's first tab is a UI file in its run folder, committed and then
    revealed (the host shows only committed files); Stop deletes it with the
    run's other tabs, and the cell keeps nothing of the session but its
    captures. No cell pin is ever touched."""
    _scripted_renderer(bridge_hook, [_toolbar("capture", _TAB)])
    res = _run_session(_session_run())
    assert res.ok, res.error

    tabs = _tab_writes(res)
    assert list(tabs) == [_TAB]
    art = tabs[_TAB]
    assert art["v"] == 4 and art["tab"] == "tab-1"
    assert art["device"] == "desktop" and art["viewport"] is None
    assert art["url"] == "https://example.com/x"
    assert re.fullmatch(r"/proxy/chan/run-1/\d+/", art["control"]["wsBase"])
    bridge = bridge_hook["bridge"]
    reveals = [c for c in res.tier_b_calls if c["op"] == "reveal_file"]
    assert reveals == [{"op": "reveal_file", "path": _TAB, "committed": True}]

    # Stop: the tab is gone, the capture stays.
    assert not any(p.startswith("_runs/") for p in res.files)
    folders = {p.split("/", 1)[0] for p in res.files}
    assert len(folders) == 1
    folder = folders.pop()
    assert folder.startswith("example.com-")
    assert res.files[f"{folder}/page.md"] == "# https://example.com/x\n"
    assert res.files[f"{folder}/screenshot.png"] == b"\x89PNG"
    assert "Session ended — 1 capture" in res.logs
    assert bridge.tracked_deletions == set()


def test_new_tab_writes_the_next_tab_and_reveals_it(bridge_hook):
    """A tab's toolbar New tab: the next `tab-<n>.webview` (blank — a new tab
    names no site), flushed and revealed — from the first tab, then from the
    tab it opened; every tab goes at Stop."""
    _scripted_renderer(bridge_hook, [
        _toolbar("new_tab", _TAB), _toolbar("new_tab", f"{_FOLDER}/tab-2.webview"),
    ])
    res = _run_session(_session_run())
    assert res.ok, res.error

    tabs = _tab_writes(res)
    assert list(tabs) == [f"{_FOLDER}/tab-{n}.webview" for n in (1, 2, 3)]
    assert [t["tab"] for t in tabs.values()] == ["tab-1", "tab-2", "tab-3"]
    assert [t["url"] for t in tabs.values()] == ["https://example.com/x", "", ""]
    # Every tab of the run shares its control socket and its login bucket.
    assert len({t["control"]["wsBase"] for t in tabs.values()}) == 1
    assert len({t["native"]["partition"] for t in tabs.values()}) == 1
    reveals = [(c["path"], c["committed"]) for c in res.tier_b_calls
               if c["op"] == "reveal_file"]
    assert reveals == [(p, True) for p in tabs]
    assert "opened tab-2" in res.logs and "opened tab-3" in res.logs
    assert not any(p.startswith("_runs/") for p in res.files)


def test_new_tab_comes_from_one_of_the_runs_tabs(bridge_hook):
    """`new_tab` is each tab's toolbar button, so its source is one of the
    run's tab files; a press naming any other file opens nothing."""
    _scripted_renderer(bridge_hook, [
        _toolbar("new_tab", "notes.md"), _toolbar("new_tab", f"{_FOLDER}/tab-7.webview"),
    ])
    res = _run_session(_session_run())
    assert res.ok, res.error
    assert list(_tab_writes(res)) == [_TAB]


def test_capture_acts_on_the_tab_it_was_clicked_for(bridge_hook):
    """A Capture press acts on the tab whose toolbar it was pressed on (the
    action's source is that tab's file). A capture naming any other file — one
    that is not a tab, a tab this run never opened — is ignored: it takes no
    capture number and writes no folder."""
    seen: list[str] = []

    async def _record(ctx):
        seen.append(ctx.state.tab)
        ctx.save("page.md", "# page\n")

    tab2 = f"{_FOLDER}/tab-2.webview"
    _scripted_renderer(bridge_hook, [
        _toolbar("new_tab", _TAB),
        _toolbar("capture", tab2),
        _toolbar("capture", "notes.md"),
        _toolbar("capture", f"{_FOLDER}/tab-7.webview"),
        _toolbar("capture", _TAB),
    ])
    res = _run_session(_session_run(on_capture=_record))
    assert res.ok, res.error
    assert seen == ["tab-2", "tab-1"]
    assert not any("capture #3" in line for line in res.logs)
    assert "Session ended — 2 captures" in res.logs


def _tab_versions(res):
    """``[(path, artifact)]`` for every tab-file write the run flushed, in
    order — every version, not just the last."""
    out: list = []
    for update in _RESULT_UPDATES[id(res)]:
        for path, content in update["files"].items():
            if path.endswith(".webview"):
                out.append((path, json.loads(content)))
    return out


def test_the_device_setting_opens_every_tab_as_that_device_GN04(bridge_hook):
    """`device=` is what every tab of the run opens as — the first and each
    one New tab adds."""
    _scripted_renderer(bridge_hook, [_toolbar("new_tab", _TAB)])
    res = _run_session(_session_run(device="tablet"))
    assert res.ok, res.error
    tabs = _tab_writes(res)
    assert len(tabs) == 2
    for art in tabs.values():
        assert art["device"] == "tablet" and art["viewport"] == {"width": 800}
        assert art["native"]["device"] == "tablet"


def test_the_device_action_rewrites_only_the_pressed_tab_GN04(bridge_hook):
    """A tab's device toggle is a `device` action whose source is that tab's
    file: the daemon rewrites THAT tab's file with the new device (same tab,
    same url, same control base) and flushes it; the other tab is untouched.
    An unknown device, a press naming no tab and a press with no payload
    change nothing."""
    tab2 = f"{_FOLDER}/tab-2.webview"
    _scripted_renderer(bridge_hook, [
        _toolbar("new_tab", _TAB),
        _toolbar("device", _TAB, {"device": "phone"}),
        _toolbar("device", tab2, {"device": "watch"}),
        _toolbar("device", "notes.md", {"device": "tablet"}),
        _toolbar("device", tab2),
        _toolbar("device", _TAB, {"device": "phone"}),
    ])
    res = _run_session(_session_run())
    assert res.ok, res.error
    versions = _tab_versions(res)
    assert [p for p, _a in versions] == [_TAB, tab2, _TAB]
    first, _second, switched = (a for _p, a in versions)
    assert first["device"] == "desktop"
    assert switched["device"] == "phone" and switched["viewport"] == {"width": 412}
    assert switched["native"] == {"partition": first["native"]["partition"], "device": "phone"}
    # Everything but the device is the tab it was: the renderer keeps its guest.
    for key in ("tab", "url", "control", "requires", "buttons"):
        assert switched[key] == first[key], key
    assert _tab_writes(res)[tab2]["device"] == "desktop"
    assert "tab-1 shows as phone" in res.logs
    assert not any("tab-2 shows as" in line for line in res.logs)
    # The toggle writes no capture and the run still ends clean.
    assert res.files == {}
    assert "Session ended — no captures" in res.logs


def test_an_undeclared_tab_type_fails_before_any_tab(bridge_hook):
    """The tab file is a declared UI file type; a manifest that does not
    declare `.webview` stops the run at its first tab rather than half-way."""
    _scripted_renderer(bridge_hook, [])
    res = _run_session(_session_run(), ui_files=[".chat"])
    assert not res.ok
    assert "ui_files" in res.error
    assert _tab_writes(res) == {}


def test_the_tab_toolbar_carries_capture_before_new_tab(bridge_hook):
    """A run with a capture callback puts Capture, under the run's label, on
    every tab's toolbar ahead of New tab — the first tab and each one New tab
    opens."""
    _p, _c, daemon = _shared()

    async def run():
        await daemon.run_web_session(
            url="https://example.com/", on_capture=_save_a_page,
            capture_button_label="Save page")

    _scripted_renderer(bridge_hook, [_toolbar("new_tab", _TAB)])
    res = _run_session(run)
    assert res.ok, res.error
    tabs = _tab_writes(res)
    assert len(tabs) == 2
    assert all(t["buttons"] == [{"id": "capture", "label": "Save page"},
                                {"id": "new_tab", "label": "New tab"}]
               for t in tabs.values())


def test_without_a_capture_callback_the_toolbar_is_new_tab_only(bridge_hook):
    """No capture callback: each tab's toolbar is New tab alone, a capture
    press is ignored, and New tab still opens tabs."""
    _p, _c, daemon = _shared()

    async def run():
        await daemon.run_web_session(url="https://example.com/")

    _scripted_renderer(bridge_hook, [_toolbar("capture", _TAB), _toolbar("new_tab", _TAB)])
    res = _run_session(run)
    assert res.ok, res.error
    assert all(t["buttons"] == [{"id": "new_tab", "label": "New tab"}]
               for t in _tab_writes(res).values())
    assert len(_tab_writes(res)) == 2   # the capture was ignored, New tab was not
    assert res.files == {}
    assert not any("capture #" in line for line in res.logs)
    assert "Session ended — no captures" in res.logs


def test_each_capture_press_gets_its_own_folder(bridge_hook, monkeypatch):
    """Two presses, two folders. The clock is stepped by hand — two presses
    inside one second would otherwise name the same folder, which is the
    timestamp's job to prevent, not this test's to observe."""
    _p, _c, daemon = _shared()
    stamps = iter(["20260101-000001", "20260101-000002"])
    monkeypatch.setattr(daemon.time, "strftime", lambda fmt: next(stamps))

    _scripted_renderer(bridge_hook, [_toolbar("capture", _TAB), _toolbar("capture", _TAB)])
    res = _run_session(_session_run())
    assert res.ok, res.error
    assert set(res.files) == {
        "example.com-20260101-000001/page.md",
        "example.com-20260101-000001/screenshot.png",
        "example.com-20260101-000002/page.md",
        "example.com-20260101-000002/screenshot.png",
    }
    assert "Session ended — 2 captures" in res.logs


def test_two_presses_in_one_second_still_get_their_own_folders(bridge_hook, monkeypatch):
    """The clock is frozen, which is the collision the timestamp cannot rule
    out on its own: three presses on one host inside one second are still three
    folders."""
    _p, _c, daemon = _shared()
    monkeypatch.setattr(daemon.time, "strftime", lambda fmt: "20260101-000001")

    _scripted_renderer(bridge_hook, [_toolbar("capture", _TAB)] * 3)
    res = _run_session(_session_run())
    assert res.ok, res.error
    assert set(res.files) == {
        "example.com-20260101-000001/page.md",
        "example.com-20260101-000001/screenshot.png",
        "example.com-20260101-000001-2/page.md",
        "example.com-20260101-000001-2/screenshot.png",
        "example.com-20260101-000001-3/page.md",
        "example.com-20260101-000001-3/screenshot.png",
    }
    assert "Session ended — 3 captures" in res.logs


def test_the_closing_count_is_captures_that_saved_not_presses(bridge_hook):
    """The closing log line may only claim captures the cell can show: a
    press that scraped nothing and a press that exploded before saving leave
    no folder behind, so neither is a capture. The per-press log lines still number the
    presses."""
    async def _flaky(ctx):
        if ctx.capture_count == 2:
            ctx.save("page.md", "# saved\n")
        elif ctx.capture_count == 3:
            raise RuntimeError("scrape exploded")
        # press 1 reaches the page and finds nothing worth saving

    _scripted_renderer(bridge_hook, [_toolbar("capture", _TAB)] * 3)
    res = _run_session(_session_run(on_capture=_flaky))
    assert res.ok, res.error
    assert "Session ended — 1 capture" in res.logs   # exactly one, not three
    assert len(res.files) == 1 and next(iter(res.files)).endswith("/page.md")
    assert any("capture #3 failed" in line for line in res.logs)


def test_a_run_that_died_says_failed_not_ended(bridge_hook):
    """An exception escaping the run loop is not Stop: the tabs still go
    (they name a dead socket either way) but the closing log line must not
    read like a clean shutdown."""
    def setup(bridge):
        bridge._autocancel_on_serve = False

        def poll(*, source="", action_id=""):
            raise RuntimeError("the run loop fell over")

        bridge.poll_renderer_action_event = poll

    bridge_hook["setup"] = setup
    res = _run_session(_session_run())
    assert not res.ok and "fell over" in res.error
    assert list(_tab_writes(res)) == [_TAB]
    assert not any(p.startswith("_runs/") for p in res.files)  # cleaned up regardless
    assert "Session failed — no captures" in res.logs


def test_stop_without_captures_leaves_an_empty_cell(bridge_hook):
    _scripted_renderer(bridge_hook, [])
    res = _run_session(_session_run())
    assert res.ok, res.error
    assert res.files == {}  # the tab is gone and nothing replaced it
    assert "Session ended — no captures" in res.logs


def test_a_failing_capture_never_ends_the_session(bridge_hook):
    async def _boom(ctx):
        ctx.save("half.md", "written before the failure\n")
        raise RuntimeError("scrape exploded")

    _scripted_renderer(bridge_hook, [_toolbar("capture", _TAB)])
    res = _run_session(_session_run(on_capture=_boom))
    assert res.ok, res.error  # the loop kept going and Stop still closed it
    assert not any(p.startswith("_runs/") for p in res.files)
    assert any(p.endswith("/half.md") for p in res.files)
    assert any("scrape exploded" in line for line in res.logs)


def test_a_session_without_a_url_opens_blank(bridge_hook):
    """No site named: the session is still a whole session — the first tab
    carries an empty url, which the renderer reads as "open blank"."""
    _scripted_renderer(bridge_hook, [])
    res = _run_session(_session_run(url=""))
    assert res.ok, res.error

    staged = [c for c in _RESULT_UPDATES[id(res)] if _TAB in c["files"]]
    assert len(staged) == 1
    artifact = json.loads(staged[0]["files"][_TAB])
    assert artifact["v"] == 4 and artifact["url"] == ""
    # …and the run says what it is rather than "Opening …"
    assert res.phases[-1] == ("running", "Live browser")
    assert staged[0]["name"] == "Web"


# ---------------------------------------------------------------------------
# The tab file — the v4 golden
# ---------------------------------------------------------------------------


def test_tab_artifact_v4_native_GN04():
    """The one construction site, pinned whole. `requires` is unconditional —
    the page is the client's own native guest view, so there is no second
    presentation to fall back to — and the keys that carried the deleted modes
    (`mode` / `image` / `live` / `controls`) must never come back. The device
    is named twice: `viewport` for the renderer's letterbox, `native.device`
    for the shell, which composes the user agent itself."""
    protocol, _c, _d = _shared()
    # A tab's file name is defined once, beside the payload, and is not a
    # caller's choice — the webview renderer claims this extension.
    assert protocol._tab_file(protocol._tab_id(1)) == "tab-1.webview"
    assert protocol._tab_file(protocol._tab_id(12)) == "tab-12.webview"
    assert protocol._tab_id(3) == "tab-3"
    art = protocol._tab_artifact(
        tab="tab-2",
        url="https://example.com/",
        proxy_base="/proxy/ch/run/9100/",
        partition=protocol._partition_for("work"),
        capture_label="Capture",
        device="phone",
    )
    assert art == {
        "v": 4,
        "tab": "tab-2",
        "url": "https://example.com/",
        "device": "phone",
        "viewport": {"width": 412},
        "control": {"wsBase": "/proxy/ch/run/9100/"},
        "requires": ["nativeGuests"],
        "native": {"partition": "persist:collimate-work", "device": "phone"},
        "buttons": [{"id": "capture", "label": "Capture"},
                    {"id": "new_tab", "label": "New tab"}],
    }
    assert ("mode" not in art and "image" not in art
            and "live" not in art and "controls" not in art)

    # A run without a capture callback: the toolbar is New tab alone, and
    # the requires gate does not depend on it.
    bare = protocol._tab_artifact(
        tab="tab-1",
        url="https://example.com/",
        proxy_base="/proxy/ch/run/9100/",
        partition=protocol._partition_for("default"),
        capture_label=None,
        device="desktop",
    )
    assert bare["buttons"] == [{"id": "new_tab", "label": "New tab"}]
    assert bare["requires"] == ["nativeGuests"]
    assert bare["native"] == {"partition": "persist:collimate-default", "device": "desktop"}
    assert bare["device"] == "desktop" and bare["viewport"] is None

    # The site is optional, and that is spelled with an empty url — the one
    # field of the artifact that may be empty. Everything else still stands.
    blank = protocol._tab_artifact(
        tab="tab-1",
        url="",
        proxy_base="/proxy/ch/run/9100/",
        partition=protocol._partition_for("default"),
        capture_label="Capture",
        device="tablet",
    )
    assert blank["v"] == 4 and blank["url"] == ""
    assert blank["viewport"] == {"width": 800}
    assert blank["control"] == {"wsBase": "/proxy/ch/run/9100/"}
    assert blank["requires"] == ["nativeGuests"]
    assert blank["buttons"] == art["buttons"]

    # The tab, the partition and the Capture label (or None) are the
    # caller's to name — no silent default.
    with pytest.raises(TypeError):
        protocol._tab_artifact(url="u", proxy_base="p", partition="x", capture_label=None,
                               device="desktop")
    with pytest.raises(TypeError):
        protocol._tab_artifact(tab="tab-1", url="u", proxy_base="p", capture_label=None,
                               device="desktop")
    with pytest.raises(TypeError):
        protocol._tab_artifact(tab="tab-1", url="u", proxy_base="p", partition="x",
                               device="desktop")
    with pytest.raises(TypeError):
        protocol._tab_artifact(tab="tab-1", url="u", proxy_base="p", partition="x",
                               capture_label=None)


def test_devices_are_desktop_phone_and_tablet_GN04():
    """The device table: desktop is the cell's full width, phone and tablet a
    fixed viewport width; an unknown name is desktop."""
    protocol, _c, _d = _shared()
    assert protocol.DEVICES == {"desktop": None, "phone": {"width": 412},
                                "tablet": {"width": 800}}
    odd = protocol._tab_artifact(tab="tab-1", url="", proxy_base="/p/",
                                 partition="persist:collimate-default",
                                 capture_label=None, device="watch")
    assert odd["device"] == "desktop" and odd["viewport"] is None
    assert odd["native"]["device"] == "desktop"


def test_normalise_url_repairs_a_typed_url_and_lets_blank_through():
    protocol, _c, _d = _shared()
    # Blank in, blank out: no site named is a session, not an error.
    assert protocol._normalise_url("") == ""
    assert protocol._normalise_url("   ") == ""
    # Anything else is still repaired and validated before the guest sees it.
    assert protocol._normalise_url("example.com") == "https://example.com"
    assert protocol._normalise_url("https:/example.com") == "https://example.com"
    assert protocol._normalise_url("HTTP:example.com") == "http://example.com"
    assert protocol._normalise_url("localhost:3000") == "https://localhost:3000"
    with pytest.raises(RuntimeError):
        protocol._normalise_url("https://")
    # Other schemes are refused rather than turned into https://ftp://...
    for raw in ("ftp://files.example", "file:///tmp/x", "about:blank",
                "javascript:alert(1)"):
        with pytest.raises(RuntimeError):
            protocol._normalise_url(raw)


def test_partition_name_is_derived_from_the_profile():
    protocol, _c, _d = _shared()
    assert protocol._partition_for("default") == "persist:collimate-default"
    assert protocol._partition_for("My Work Profile!") == "persist:collimate-my-work-profile"
    assert protocol._partition_for("") == "persist:collimate-default"


# ---------------------------------------------------------------------------
# Manifest / settings / runner surface
# ---------------------------------------------------------------------------


def test_manifest_shape():
    with open(os.path.join(_WEB_DIR, "web.generator"), encoding="utf-8") as fh:
        man = yaml.safe_load(fh)
    assert man["id"] == "web"
    assert man["entrypoint"] == "web.py"
    assert man["daemon"] is True
    # Its interface is its tabs, and nothing else lives in its run folder.
    assert man["ui_files"] == [".webview"]
    # The host writes the first tab as a starting placeholder the moment the
    # run is accepted, so the tab shows before the daemon exists (GN-03).
    assert man["opens"] == [{"path": "{run}/tab-1.webview"}]
    # A session is an ordinary cell now: it keeps its captures, so it is not
    # throwaway and Stop is the only stop affordance there is.
    assert "ephemeral" not in man
    # The site is optional — the prompt names one, or nothing does.
    assert man["prompt_required"] is False and man["prompt_intent"]
    # …and so are the two blocks a browser subprocess needed: nothing is
    # launched here, so the host defaults are ample.
    assert "rlimits" not in man and "env_passthrough" not in man
    folders = {f["name"]: f for f in man["config_folders"]}
    assert "capture_scripts" in folders
    assert folders["capture_scripts"]["sample"] == "samples/capture_scripts"
    assert os.path.isdir(os.path.join(_WEB_DIR, "samples", "capture_scripts"))


def test_settings_are_the_profile_and_the_device_GN04():
    with open(os.path.join(_WEB_DIR, "settings.set_yaml"), encoding="utf-8") as fh:
        fields = {f["key"]: f for f in yaml.safe_load(fh)["fields"]}
    # One session shape, two settings: the login bucket and the device.
    assert list(fields) == ["profile", "device"]
    device = fields["device"]
    assert device["type"] == "dropdown" and device["label"] == "Device"
    assert device["default"] == "desktop"
    assert [o["value"] for o in device["options"]] == ["desktop", "phone", "tablet"]
    assert [o["label"] for o in device["options"]] == ["Desktop", "Phone", "Tablet"]
    assert fields["profile"]["type"] == "text"
    assert fields["profile"]["default"] == "default"
    desc = fields["profile"]["description"]
    # It names the guest's persistent partition — and says two names can be
    # signed in at once, which the old Chrome-profile lock could not.
    assert "persist:collimate-<name>" in desc
    assert "at the same time" in desc
    assert "Chrome" not in desc


def test_web_group_and_runner_lint_clean():
    issues = lint_path(os.path.join(GENERATORS, "web"))
    problems = [i.format() for i in issues if i.level in (Level.ERROR, Level.WARNING)]
    assert not problems, problems


def test_web_configs_are_complete_templates__F1879():
    root=GENERATORS / "web/web/configs"
    for name in ("default", "web", "web--capture-chat"):
        runner=yaml.safe_load((root/name/"manifest.config_yaml").read_text())
        assert set(runner["settings"])=={"settings.set_yaml"}
        assert runner["settings"]["settings.set_yaml"]=={"profile": "default", "device": "desktop"}
        assert runner["permissions"]=={"internet":"full","docker":"none"}
    assert (root/"web--capture-chat/files/capture_scripts/claude.ai.py").is_file()


def test_webview_producer_publishes_toolbar_buttons__F1765():
    protocol, _c, _d = _shared()
    artifact = protocol._tab_artifact(tab="tab-7", url="https://example.com/",
                                      proxy_base="/proxy/ch/run/9100/",
                                      partition="persist:collimate-work",
                                      capture_label="Capture", device="desktop")
    assert set(artifact) == {"v", "tab", "url", "device", "viewport", "control", "requires",
                             "native", "buttons"}
    assert artifact["buttons"] == [{"id": "capture", "label": "Capture"},
                                   {"id": "new_tab", "label": "New tab"}]
    artifact["buttons"][-1]["label"] = "local mutation"
    fresh = protocol._tab_artifact(tab="tab-8", url="", proxy_base="/proxy/ch/run/9100/",
                                   partition="persist:collimate-work", capture_label=None,
                                   device="desktop")
    assert fresh["buttons"] == [{"id": "new_tab", "label": "New tab"}]


def test_failed_capture_commits_and_counts_its_partial_files_immediately__F1635(bridge_hook):
    async def partial(ctx):
        ctx.save('partial.txt', 'kept from failed press')
        raise RuntimeError('failed after first file')
    _scripted_renderer(bridge_hook, [_toolbar('capture', _TAB)])
    before_stop = []
    original_setup = bridge_hook['setup']
    def setup(bridge):
        original_setup(bridge)
        original_poll = bridge.poll_renderer_action_event
        def poll(**kwargs):
            event = original_poll(**kwargs)
            if event is None:
                before_stop.append(any(any(path.endswith('/partial.txt') for path in update['files']) for update in bridge.updates))
            return event
        bridge.poll_renderer_action_event = poll
    bridge_hook['setup'] = setup
    result = _run_session(_session_run(on_capture=partial))
    assert result.ok, result.error
    updates = _RESULT_UPDATES[id(result)]
    partial_updates = [update for update in updates if any(path.endswith('/partial.txt') for path in update['files'])]
    assert partial_updates and before_stop == [True]
    assert any('partial' in line and 'capture #1' in line for line in result.logs)
    assert 'Session ended — 1 capture' in result.logs


def test_stop_cancels_stalled_capture_and_keeps_partial_before_cleanup__F1636(bridge_hook):
    settled = []
    async def stalled(ctx):
        ctx.save('partial.txt', 'kept before Stop')
        bridge_hook['bridge']._signal_stop()
        try:
            await asyncio.Future()
        finally:
            settled.append('joined')
    _scripted_renderer(bridge_hook, [_toolbar('capture', _TAB)])
    result = run_generator_callable(_session_run(on_capture=stalled), label=_LABEL, channel='chan', run_id='run-1', ui_files=['.webview'], timeout=0.8)
    assert result.ok, result.error
    assert settled == ['joined']
    assert any(path.endswith('/partial.txt') for path in result.files)
    assert not any(path.endswith('.webview') for path in result.files)
    assert 'Session ended — 1 capture' in result.logs
    assert any('partial' in line and 'capture #1' in line for line in result.logs)


@pytest.mark.parametrize('url', ['about:blank', 'example.org/path', ''])
def test_current_url_host_uses_absolute_url_rule_for_capture_and_dispatch__F1661(url):
    _, _, daemon = _shared()
    assert web._hostname(url) == ''
    assert daemon._capture_dir_name(url).startswith('page-')


@pytest.mark.parametrize('phase', ['current_url', 'body'])
def test_stop_joins_real_capture_rpc_and_removes_pending_reply__F1636(native_bridge, phase):
    protocol, capture, daemon = _shared()
    async def scenario():
        class Peer:
            closed = False
            async def send_str(self, message):
                native_bridge._signal_stop()
        state = protocol._TabState(tab='tab-1', path='tab-1.webview', current_url='https://example.org/')
        state.client = Peer()
        state.client_caps = {'guest_capture': True}
        ctx = capture.CaptureContext(url=state.current_url, capture_count=1, state=state)
        called = []
        async def body(current):
            called.append(True)
            await current.html()
        if phase == 'body':
            async def url():
                return ctx.url
            ctx.current_url = url
        stopped = await asyncio.wait_for(daemon._capture_until_stop(ctx, state, set(), body), 1)
        assert stopped
        assert not state.rpc_pending
        assert called == ([True] if phase == 'body' else [])
    asyncio.run(scenario())


def test_external_task_cancellation_keeps_partial_then_propagates__F1636(bridge_hook):
    from contextlib import suppress
    seen = []
    async def capture(ctx):
        ctx.save('partial.txt', 'external task cancellation')
        seen.append(asyncio.current_task())
        await asyncio.Future()
    _scripted_renderer(bridge_hook, [_toolbar('capture', _TAB)])
    async def run():
        task = asyncio.create_task(_session_run(on_capture=capture)())
        while not seen:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with suppress(asyncio.CancelledError):
            await seen[0]
        assert seen[0].done()
    result = _run_session(run)
    assert result.ok, result.error
    assert any(path.endswith('/partial.txt') for path in result.files)
    assert 'Session ended — 1 capture' in result.logs
