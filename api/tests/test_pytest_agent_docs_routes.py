"""The agent docs and the test harnesses name only routes the app serves.

Every ``*.md`` under ``docs/ai_tools/`` (the index agents read first, the
notes and the skills) and ``docs/reference/`` is what an agent reads to seed,
drive and reason about the app. After the blob routes moved under
``/api/channels/{channel}/``, the index and a seed recipe still sent cell
bodies to the unscoped ``POST /api/blob/stream``, which answers 404, and so
did ``persistence_browser.py``'s cell import. Every backticked
``VERB /api/...`` (or ``VERB/VERB /api/...``) in those docs must match a
route the app registers (a ``{name}`` in either is any one path segment).
The notes are hard-wrapped, so a span may break between its verb and its
path: each doc is read with its line breaks and indentation folded into
single spaces.

History convention: a mention directly preceded by the word ``retired``
("the retired ``POST /api/blob``") is a record of a route the app no longer
serves, and is not checked. Only that mention is exempt; every other mention,
on the same line or not, is a live claim.

The harness modules beside the tests (``api/tests``, its ``helpers`` and
``api/tests_live``, not the ``test_*`` files, which also name refused paths
on purpose) drive the app over HTTP: every ``/api/...`` string they build,
f-string fields read as one segment and a query string cut off, must be a
path the app serves.

``docs/`` is not part of the public mirror (``.github/sync-exclude.txt``),
so the docs tests skip where it is absent.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AI_TOOLS = REPO / "docs" / "ai_tools"
REFERENCE = REPO / "docs" / "reference"
HARNESS_DIRS = (REPO / "api" / "tests", REPO / "api" / "tests" / "helpers", REPO / "api" / "tests_live")

needs_docs = pytest.mark.skipif(
    not AI_TOOLS.is_dir(), reason="docs/ is not part of the public mirror",
)

_VERB = r"(?:GET|POST|PUT|PATCH|DELETE)"
_MENTION = re.compile(
    r"(?P<retired>\b(?i:retired) )?`(?P<verbs>" + _VERB + r"(?:/" + _VERB + r")*) (?P<path>/api/[^`\s?]*)",
)
_PARAM = re.compile(r"\\\{[^}]*\\\}")


def _agent_docs() -> list[Path]:
    return sorted(
        path for root in (AI_TOOLS, REFERENCE) for path in root.rglob("*.md")
    )


def _mentions(text: str) -> list[tuple[str, str, bool]]:
    """``(verb, path, retired)`` for each backticked route in ``text``, with
    its line breaks and indentation folded into single spaces."""
    folded = re.sub(r"\s+", " ", text)
    return [
        (verb, match.group("path").rstrip(".,;:)"), bool(match.group("retired")))
        for match in _MENTION.finditer(folded)
        for verb in match.group("verbs").split("/")
    ]


def _live_mentions(doc: Path) -> list[tuple[str, str]]:
    return [
        (verb, path)
        for verb, path, retired in _mentions(doc.read_text(encoding="utf-8"))
        if not retired
    ]


def _served(app) -> list[tuple[str, re.Pattern[str]]]:
    from fastapi.routing import APIRoute, iter_route_contexts

    served = []
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute):
            continue
        path = re.sub(r"\{(\w+):\w+\}", r"{\1}", route.path)
        pattern = re.compile("^" + _PARAM.sub(r"[^/]+", re.escape(path)) + "$")
        served.extend((method, pattern) for method in route.methods)
    return served


@needs_docs
def test_agent_docs_name_only_served_routes(api_app):
    served = _served(api_app)
    stale = [
        f"{doc.relative_to(REPO)}: {verb} {path}"
        for doc in _agent_docs()
        for verb, path in _live_mentions(doc)
        if not any(verb == m and pattern.match(path) for m, pattern in served)
    ]
    assert not stale, "routes the app does not serve:\n" + "\n".join(stale)


@needs_docs
def test_the_check_reads_every_agent_doc():
    docs = _agent_docs()
    assert AI_TOOLS / "README.md" in docs
    assert any(doc.is_relative_to(AI_TOOLS / "notes") for doc in docs)
    assert any(doc.is_relative_to(AI_TOOLS / "skills") for doc in docs)
    assert any(doc.is_relative_to(REFERENCE) for doc in docs)
    mentions = [mention for doc in docs for mention in _live_mentions(doc)]
    assert any(path.startswith("/api/channels/") for _, path in mentions)


def test_a_mention_wrapped_between_verb_and_path_is_read():
    text = "the panel posts to `POST\n  /api/blob/stream` with each body"
    assert _mentions(text) == [("POST", "/api/blob/stream", False)]


def test_only_the_mention_retired_precedes_is_history():
    text = ("bodies go to `POST /api/channels/{chq}/blob/stream` (the retired\n"
            "`POST /api/blob` answered 404) and read from `GET /api/blob/{hash}`")
    assert _mentions(text) == [
        ("POST", "/api/channels/{chq}/blob/stream", False),
        ("POST", "/api/blob", True),
        ("GET", "/api/blob/{hash}", False),
    ]
    assert _mentions("the retired `POST/GET /api/accounts/login`") == [
        ("POST", "/api/accounts/login", True), ("GET", "/api/accounts/login", True),
    ]


def _harness_modules() -> list[Path]:
    return sorted(
        path for folder in HARNESS_DIRS for path in folder.glob("*.py")
        if not path.name.startswith("test_")
    )


def _api_paths(module: Path) -> list[tuple[int, str]]:
    """``(line, path)`` for each string literal in ``module`` that starts
    with ``/api/``: an f-string's fields read as ``{field}`` (one segment),
    the query string cut off."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    parts = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
             for part in node.values}
    found = []
    for node in ast.walk(tree):
        if id(node) in parts:
            continue  # a piece of an f-string, read with it
        if isinstance(node, ast.JoinedStr):
            text = "".join(part.value if isinstance(part, ast.Constant) else "{field}"
                           for part in node.values)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        else:
            continue
        if text.startswith("/api/") and not re.search(r"\s", text):
            found.append((node.lineno, text.split("?", 1)[0]))
    return found


def test_harness_modules_name_only_served_paths(api_app):
    patterns = [pattern for _, pattern in _served(api_app)]
    modules = _harness_modules()
    assert REPO / "api" / "tests" / "persistence_browser.py" in modules
    checked = [(module, line, path) for module in modules for line, path in _api_paths(module)]
    assert any(path.endswith("/blob/stream") for _, _, path in checked)
    stale = [
        f"{module.relative_to(REPO)}:{line}: {path}"
        for module, line, path in checked
        if not any(pattern.match(path) for pattern in patterns)
    ]
    assert not stale, "paths the app does not serve:\n" + "\n".join(stale)
