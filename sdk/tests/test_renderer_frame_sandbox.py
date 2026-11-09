# SPDX-License-Identifier: MIT
"""Frame sandbox contract over every shipped renderer entrypoint.

A native renderer (``embed: native``) runs in the app's own document, so an
``<iframe>`` it renders is a frame inside the app. What that frame may do is
decided by its ``sandbox`` attribute:

- every frame carries one, as a string literal (so this file, and a reader,
  can see exactly what it grants);
- no frame grants any ``allow-top-navigation*`` token, so a page on another
  origin can never replace the app's tab (F1738);
- ``allow-scripts`` together with ``allow-same-origin`` lets a document on the
  app's origin lift its own sandbox and script the app, so the frames that
  combine them are a fixed, listed set (``_SAME_ORIGIN_SCRIPTED``), each tied
  to the JSX condition it renders under. A new one fails here.

The listed set is what the renderers do now. The webview's browser fallback
renders its frame only when ``fallbackPane`` says ``'frame'``: the URL it is
given is an absolute http(s) URL on another origin
(``builtins/live/webview/artifact.test.ts`` pins that). That checks only the
URL the frame is given; the framed site can still navigate its own frame onto
the app's origin. The website renderer draws two frames under one condition
(F1739): the one with both tokens only when ``frameKeepsOrigin`` allows the
address, which is another site or a ``/proxy/`` address on the app server (a
live run's served page, trusted because the user started the run and opened
its page); every other address on the app server gets the frame without
``allow-same-origin`` (``builtins/live/website/frame.test.ts`` pins the rule).

Pure text analysis of the JSX, like ``test_renderer_cdn_pins.py``: no Node.
"""
from __future__ import annotations

import glob
import os
import re
from collections import Counter
from typing import NamedTuple

from tests._workspace import WORKSPACE
from tests.conftest import RENDERER_ROOTS

# Every entrypoint that renders a frame → how many it renders. A frame the
# scan misreads as a comment (it would escape every check below) or a new
# frame changes this map.
_FRAME_COUNTS = {
    "builtins/live/webview/main.tsx": 1,
    "builtins/live/website/main.tsx": 2,
    "content/renderers/visual/html/main.tsx": 1,
}

# The frames that hold allow-scripts + allow-same-origin, one per listed
# entrypoint → the JSX condition the source must end with right before the
# tag (None: the frame is listed as it is).
_SAME_ORIGIN_SCRIPTED: dict[str, str | None] = {
    # The browser fallback: rendered only for fallbackPane's 'frame'.
    "builtins/live/webview/main.tsx": r"\{\s*pane\s*===\s*'frame'\s*\?\s*\(\s*$",
    # The .website frame for another site or a live run's /proxy page (F1739).
    "builtins/live/website/main.tsx": r"\)\s*:\s*keepsOrigin\s*\?\s*\(\s*$",
}

_SANDBOX = re.compile(r"\bsandbox\s*=\s*(\"([^\"]*)\"|'([^']*)'|\{)")


class _Frame(NamedTuple):
    path: str
    tag: str
    before: str  # the entrypoint's source up to the tag


def _entrypoints() -> list[str]:
    found: list[str] = []
    for root in RENDERER_ROOTS:
        for ext in ("tsx", "ts", "js"):
            found += glob.glob(os.path.join(root, "*", "*", f"main.{ext}"))
    return sorted(found)


def _rel(path: str) -> str:
    return os.path.relpath(path, WORKSPACE)


def _line_state(fragment: str) -> str:
    """Where a one-line fragment ends: ``code``, ``string`` (a quote left
    open) or ``comment`` (after a ``//`` outside quotes)."""
    quote = None
    i = 0
    while i < len(fragment):
        ch = fragment[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif fragment.startswith("//", i):
            return "comment"
        i += 1
    return "code" if quote is None else "string"


def _in_comment(src: str, at: int) -> bool:
    """Whether offset ``at`` sits in a comment (prose that names ``<iframe``).

    Errs toward code: a frame misread as prose would escape every check, while
    prose misread as a frame fails loudly. So a line counts as a comment only
    when it starts as one, and a ``/*`` inside a quoted string or after a
    ``//`` on its line ('image/*', a glob in prose) opens nothing."""
    line_start = src.rfind("\n", 0, at) + 1
    if src[line_start:at].lstrip().startswith(("//", "*", "/*")):
        return True
    end = at
    while True:
        opener = src.rfind("/*", 0, end)
        if opener < 0:
            return False
        if _line_state(src[src.rfind("\n", 0, opener) + 1:opener]) == "code":
            break
        end = opener
    return src.find("*/", opener + 2, at) < 0


def _iframe_tags(src: str) -> list[tuple[int, str]]:
    """``(offset, opening tag)`` of every JSX ``<iframe`` in ``src`` that is
    not in a comment.

    Walks from ``<iframe`` to the ``>`` that closes the tag at brace depth 0,
    stepping over string literals, template literals, ``{…}`` expressions
    (an arrow's ``=>`` is not the tag's end) and comments (an apostrophe in a
    comment is not a string)."""
    tags: list[tuple[int, str]] = []
    for m in re.finditer(r"<iframe(?=[\s/>])", src):
        if _in_comment(src, m.start()):
            continue
        i, depth, out = m.end(), 0, ["<iframe"]
        while i < len(src):
            ch, two = src[i], src[i:i + 2]
            if two == "//":
                i = src.find("\n", i)
                i = len(src) if i < 0 else i
                continue
            if two == "/*":
                end = src.find("*/", i + 2)
                i = len(src) if end < 0 else end + 2
                continue
            if ch in "\"'`":
                j = i + 1
                while j < len(src) and src[j] != ch:
                    j += 2 if src[j] == "\\" else 1
                out.append(src[i:j + 1])
                i = j + 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif depth == 0 and (two == "/>" or ch == ">"):
                out.append(src[i:i + (2 if two == "/>" else 1)])
                break
            out.append(ch)
            i += 1
        tags.append((m.start(), "".join(out)))
    return tags


def _frames() -> list[_Frame]:
    frames: list[_Frame] = []
    for path in _entrypoints():
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        frames += [_Frame(_rel(path), tag, src[:at]) for at, tag in _iframe_tags(src)]
    return frames


def _tokens(tag: str) -> list[str] | None:
    """The sandbox tokens of a tag, or None when it has no literal sandbox."""
    m = _SANDBOX.search(tag)
    if m is None or m.group(1) == "{":
        return None
    value = m.group(2) if m.group(2) is not None else m.group(3)
    return value.split()


def test_frame_scan_finds_every_frame():
    """Guard the guard (F1738): the scan sees exactly the frames the renderers draw."""
    assert dict(Counter(f.path for f in _frames())) == _FRAME_COUNTS


def test_iframe_tag_scan_steps_over_comments_and_expressions():
    """The scan behind the F1738 checks reads prose as prose and a tag to its end."""
    src = (
        "// a bare <iframe> named in prose is not a frame\n"
        "/* nor <iframe src=x> here */\n"
        "/**\n * nor <iframe> in a doc comment\n */\n"
        "<iframe\n"
        "  onLoad={() => go('>')}\n"
        "  // the app's tab: allow-top-navigation stays off\n"
        '  sandbox="allow-scripts"\n'
        "/>\n"
    )
    [(at, tag)] = _iframe_tags(src)
    assert src[at:].startswith("<iframe\n")
    assert tag.endswith("/>")
    assert "allow-top-navigation" not in tag
    assert _tokens(tag) == ["allow-scripts"]


def test_iframe_tag_scan_sees_frames_after_a_slash_star_in_a_string():
    """A ``/*`` inside a string or after a ``//`` opens no comment, so the
    frames after it are still checked (F1738)."""
    for prefix in (
        "const glob = 'assets/*.png';\n",
        'const accept = "image/*";\n',
        "// files under src/* are served as they are\n",
    ):
        src = prefix + "export const V = () => <iframe src={u} />;\n"
        assert [tag for _, tag in _iframe_tags(src)] == ["<iframe src={u} />"], prefix


def test_every_renderer_iframe_is_sandboxed__F1738():
    """Every frame a shipped renderer draws carries a literal sandbox."""
    bad = [f"  {f.path}:\n    {f.tag[:160]}" for f in _frames() if _tokens(f.tag) is None]
    assert not bad, (
        "renderer <iframe>(s) without a string-literal sandbox attribute — a "
        "native renderer's frame sits in the app document, so an unsandboxed "
        "one can navigate the app tab and open popups freely:\n" + "\n".join(bad)
    )


def test_no_renderer_iframe_grants_top_navigation__F1738():
    """A page on another origin can never replace the app's tab, gesture or not."""
    bad = [
        f"  {f.path}: {' '.join(tokens)}"
        for f in _frames()
        if (tokens := _tokens(f.tag)) and any(t.startswith("allow-top-navigation") for t in tokens)
    ]
    assert not bad, "renderer frame(s) granting top navigation:\n" + "\n".join(bad)


def test_scripted_same_origin_frames_are_only_the_listed_ones__F1738():
    """allow-scripts + allow-same-origin only in the listed frames, each under
    its listed condition (the webview fallback's under ``pane === 'frame'``)."""
    counts: Counter[str] = Counter()
    unguarded: list[str] = []
    for f in _frames():
        tokens = _tokens(f.tag) or []
        if "allow-scripts" not in tokens or "allow-same-origin" not in tokens:
            continue
        counts[f.path] += 1
        guard = _SAME_ORIGIN_SCRIPTED.get(f.path)
        if guard is not None and not re.search(guard, f.before):
            unguarded.append(f"  {f.path}: not rendered under /{guard}/")
    assert dict(counts) == dict.fromkeys(_SAME_ORIGIN_SCRIPTED, 1), (
        "renderer frame(s) combining allow-scripts and allow-same-origin "
        f"differ from the listed set {sorted(_SAME_ORIGIN_SCRIPTED)}: {dict(counts)}. "
        "A document on the app's origin in such a frame can remove its own "
        "sandbox and script the app; frame app-origin content (the /proxy "
        "path, cell files) without allow-same-origin."
    )
    assert not unguarded, (
        "a listed frame lost the condition it renders under:\n" + "\n".join(unguarded)
    )
