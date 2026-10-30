# SPDX-License-Identifier: MIT
"""collimate._host.module_mirror: keys, resolution, body rewriting, roots, licenses.

Pure functions only, no network. The committed snapshot itself is
``test_module_snapshot.py``.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from collimate._host.esm_lexer import LexError, scan_imports
from collimate._host.module_mirror import (
    CONTRACT, ESM_SH_POLYFILLS, HOSTS, UPSTREAMS, InvalidKey, canonical_url, check_redirect,
    is_floating, is_javascript, license_allowed, mirror_key, package_of, read_mirror_assets,
    relative, resolve, rewrite_module, served_body, shipped_roots,
)

from tests._workspace import BUILTINS, GENERATORS, RENDERERS, WORKSPACE

ROOT = Path(__file__).resolve().parents[1]


# -- keys ---------------------------------------------------------------------
def test_contract_names_both_upstreams():
    assert UPSTREAMS == ("https://esm.sh/", "https://cdn.jsdelivr.net/")
    assert HOSTS == ("esm.sh", "cdn.jsdelivr.net")


@pytest.mark.parametrize("raw_path, raw_query, key", [
    ("esm.sh/react@19.2.0", "target=es2022", "https://esm.sh/react@19.2.0?target=es2022"),
    ("esm.sh/prosemirror-view@%5E1.41.3", "deps=a@1&target=es2022",
     "https://esm.sh/prosemirror-view@^1.41.3?deps=a@1&target=es2022"),
    ("esm.sh/prosemirror-view@^1.41.3", "deps=a@1&target=es2022",
     "https://esm.sh/prosemirror-view@^1.41.3?deps=a@1&target=es2022"),
    ("esm.sh/a%7Bb%7D%60", "", "https://esm.sh/a{b}`"),
    ("esm.sh/a%2bb%3d", "q=%2b%26", "https://esm.sh/a%2Bb%3D?q=%2B%26"),
    ("esm.sh/a b", "", "https://esm.sh/a%20b"),
    ("esm.sh/a%22b\"", "", "https://esm.sh/a%22b%22"),
    ("esm.sh/caf%C3%A9", "", "https://esm.sh/caf%C3%A9"),
    ("esm.sh/café", "", "https://esm.sh/caf%C3%A9"),
    ("esm.sh/x", "", "https://esm.sh/x"),
    ("cdn.jsdelivr.net/npm/katex@0.17.0/dist/katex.min.css", "",
     "https://cdn.jsdelivr.net/npm/katex@0.17.0/dist/katex.min.css"),
    ("esm.sh/q", "a=%2F&b=/", "https://esm.sh/q?a=%2F&b=/"),
])
def test_mirror_key_canonicalizes(raw_path, raw_query, key):
    assert mirror_key(raw_path, raw_query) == key
    assert canonical_url(key) == key


def test_query_order_is_part_of_the_key():
    assert mirror_key("esm.sh/x@1.0.0", "deps=a@1,b@2") != mirror_key("esm.sh/x@1.0.0", "deps=b@2,a@1")


@pytest.mark.parametrize("raw_path, raw_query", [
    ("example.com/x", ""), ("ESM.sh/x", ""), ("esm.sh", ""), ("esm.sh/", ""), ("/esm.sh/x", ""),
    ("esm.sh/a%2Fb", ""), ("esm.sh/a%2fb", ""), ("esm.sh/a%5Cb", ""), ("esm.sh/a\\b", ""),
    ("esm.sh/a/../b", ""), ("esm.sh/./a", ""), ("esm.sh/%2e%2E/a", ""), ("esm.sh/a/%2E", ""),
    ("esm.sh/a%00", ""), ("esm.sh/a\0", ""), ("esm.sh/a#b", ""), ("esm.sh/a", "b#c"),
    ("esm.sh/a%zz", ""), ("esm.sh/a%2", ""), ("esm.sh/a?b", ""), ("esm.sh/a", "q=\\"),
])
def test_mirror_key_refuses(raw_path, raw_query):
    with pytest.raises(InvalidKey):
        mirror_key(raw_path, raw_query)


@pytest.mark.parametrize("url, key", [
    ("https://ESM.SH/x@1.0.0", "https://esm.sh/x@1.0.0"),
    ("https://esm.sh:443/x@1.0.0", "https://esm.sh/x@1.0.0"),
    ("HTTPS://cdn.jsdelivr.net/npm/a@1.0.0/b.js?", "https://cdn.jsdelivr.net/npm/a@1.0.0/b.js"),
])
def test_canonical_url(url, key):
    assert canonical_url(url) == key


@pytest.mark.parametrize("url", [
    "http://esm.sh/x", "https://user@esm.sh/x", "https://esm.sh:8443/x", "https://unpkg.com/x", "esm.sh/x",
])
def test_canonical_url_refuses(url):
    with pytest.raises(InvalidKey):
        canonical_url(url)


# -- resolution -----------------------------------------------------------------
BASE = "https://esm.sh/@milkdown/crepe@7.21.2/X-ZGVwcw/es2022/crepe.mjs"


@pytest.mark.parametrize("specifier, target", [
    ("/react@19.2.0/es2022/react.mjs", "https://esm.sh/react@19.2.0/es2022/react.mjs"),
    ("./feature/a.mjs", "https://esm.sh/@milkdown/crepe@7.21.2/X-ZGVwcw/es2022/feature/a.mjs"),
    ("../../../../x@1.0.0", "https://esm.sh/x@1.0.0"),
    ("../../../../../../x@1.0.0", "https://esm.sh/x@1.0.0"),
    ("/../x@1.0.0", "https://esm.sh/x@1.0.0"),
    ("/prosemirror-view@%5e1.41.3?deps=prosemirror-model@1.25.11&target=es2022",
     "https://esm.sh/prosemirror-view@^1.41.3?deps=prosemirror-model@1.25.11&target=es2022"),
    ("https://esm.sh/x@1.0.0?target=es2022", "https://esm.sh/x@1.0.0?target=es2022"),
    ("//esm.sh/x@1.0.0", "https://esm.sh/x@1.0.0"),
    ("https://cdn.jsdelivr.net/npm/a@1.0.0/+esm", "https://cdn.jsdelivr.net/npm/a@1.0.0/+esm"),
    ("/a\\b.mjs", "https://esm.sh/a/b.mjs"),
    ("react", None), ("@scope/pkg", None), ("data:text/javascript,export{}", None),
    ("blob:https://app/x", None), ("http://esm.sh/x", None), ("https://unpkg.com/x@1.0.0", None),
    ("node:fs", None), ("..\\x", None), ("https://example.com/x.js#frag", None),
])
def test_resolve(specifier, target):
    assert resolve(specifier, BASE) == target


def test_resolve_refuses_an_upstream_fragment():
    with pytest.raises(InvalidKey):
        resolve("/x@1.0.0#frag", BASE)


@pytest.mark.parametrize("source, target, expected", [
    ("https://esm.sh/a/b/c.mjs", "https://esm.sh/a/b/d.mjs", "./d.mjs"),
    ("https://esm.sh/a/b/c.mjs", "https://esm.sh/a/x/d.mjs", "../x/d.mjs"),
    ("https://esm.sh/a/b/c.mjs", "https://esm.sh/react@19.2.0/es2022/react.mjs",
     "../../react@19.2.0/es2022/react.mjs"),
    ("https://esm.sh/react@19.2.0?target=es2022", "https://esm.sh/react@19.2.0/es2022/react.mjs",
     "./react@19.2.0/es2022/react.mjs"),
    ("https://esm.sh/a/b/c.mjs", "https://esm.sh/a?x=1", "../../a?x=1"),
    ("https://esm.sh/a/b/c.mjs", "https://esm.sh/a/b/", "./"),
    ("https://esm.sh/a/b/c.mjs", "https://esm.sh/a/b", "../b"),
    ("https://esm.sh/a/b/c.mjs", "https://esm.sh/x:y", "../../x:y"),
])
def test_relative(source, target, expected):
    assert relative(source, target) == expected
    assert resolve(expected, source) == target


def test_relative_refuses_across_hosts():
    with pytest.raises(ValueError):
        relative("https://esm.sh/a", "https://cdn.jsdelivr.net/b")


# -- rewriting ------------------------------------------------------------------
BODY = (
    'import a from "/react@19.2.0/es2022/react.mjs";\n'
    "import './local.mjs';\n"
    'export * from "../../dep@1.0.0/es2022/dep.mjs";\n'
    "import b from 'https://esm.sh/x@1.0.0?target=es2022';\n"
    "import c from 'https://cdn.jsdelivr.net/npm/c@1.0.0/+esm';\n"
    "import d from '/@scope/floating@^1.0.0?target=es2022';\n"
    "const e = () => import(`/lazy@1.0.0/es2022/lazy.mjs`);\n"
    "const f = () => import(name);\n"
    "import g from 'data:text/javascript,export default 1';\n"
    "import h from 'bare-package';\n"
    "import i from '../../../../../../../climbs.mjs';\n"
    "import j from '//esm.sh/proto@1.0.0';\n"
)
URL = "https://esm.sh/@a/pkg@1.0.0/es2022/pkg.mjs"


def test_rewrite_makes_own_host_specifiers_relative():
    rewrite = rewrite_module(BODY.encode(), url=URL)
    text = rewrite.body.decode()
    assert 'from "../../../react@19.2.0/es2022/react.mjs"' in text
    assert "import './local.mjs'" in text  # relative, kept byte for byte
    assert 'from "../../dep@1.0.0/es2022/dep.mjs"' in text  # kept
    assert "from '../../../x@1.0.0?target=es2022'" in text
    assert "from 'https://cdn.jsdelivr.net/npm/c@1.0.0/+esm'" in text  # the import map routes it
    assert "from '../../../@scope/floating@^1.0.0?target=es2022'" in text
    assert "import(`../../../lazy@1.0.0/es2022/lazy.mjs`)" in text
    assert "from '../../../climbs.mjs'" in text  # clamped upstream, so rebased
    assert "from '../../../proto@1.0.0'" in text
    assert rewrite.computed == 1
    assert rewrite.external == ("bare-package", "data:text/javascript,export default 1")
    assert "https://cdn.jsdelivr.net/npm/c@1.0.0/+esm" in rewrite.imports
    assert rewrite.dynamic == ("https://esm.sh/lazy@1.0.0/es2022/lazy.mjs",)


def _targets(body: bytes, base: str) -> list[str | None]:
    return [resolve(r.name, base) if r.name else None for r in scan_imports(body.decode())]


@pytest.mark.parametrize("final_url", [None, "https://esm.sh/@a/pkg@1.0.0/es2022/other/deeper/pkg.mjs",
                                       "https://esm.sh/moved@2.0.0/pkg.mjs"])
def test_rewritten_specifiers_resolve_against_the_served_url_as_the_originals_did(final_url):
    """resolve(new, served url) == resolve(old, final url), specifier by specifier."""
    rewrite = rewrite_module(BODY.encode(), url=URL, final_url=final_url)
    assert _targets(rewrite.body, URL) == _targets(BODY.encode(), final_url or URL)


def test_redirect_rebases_relative_specifiers():
    body = b"import './sibling.mjs';"
    rewrite = rewrite_module(body, url="https://esm.sh/a@1.0.0/x.mjs", final_url="https://esm.sh/a@1.0.0/es2022/x.mjs")
    assert rewrite.body == b"import './es2022/sibling.mjs';"
    assert rewrite.imports == ("https://esm.sh/a@1.0.0/es2022/sibling.mjs",)


def test_cross_host_relative_specifier_becomes_absolute():
    rewrite = rewrite_module(b"import './b.js';", url="https://esm.sh/a@1.0.0/x.mjs",
                             final_url="https://cdn.jsdelivr.net/npm/a@1.0.0/x.js")
    assert rewrite.body == b"import 'https://cdn.jsdelivr.net/npm/a@1.0.0/b.js';"


def test_unchanged_body_is_the_same_bytes():
    raw = b"import './a.mjs';\nexport const x = 1;\n"
    assert rewrite_module(raw, url=URL).body is raw


def test_a_key_quote_is_escaped_in_the_literal():
    rewrite = rewrite_module(b"import '/it%27s@1.0.0';", url=URL)
    assert rewrite.body == b"import '../../../it%27s@1.0.0';"
    assert _targets(rewrite.body, URL) == ["https://esm.sh/it's@1.0.0"]


def test_byte_order_mark_is_kept_and_skipped():
    raw = "﻿import '/x@1.0.0';".encode()
    assert rewrite_module(raw, url=URL).body == "﻿import '../../../x@1.0.0';".encode()


def test_rewrite_refuses_bad_bodies():
    with pytest.raises(ValueError):
        rewrite_module(b"\xff\xfe", url=URL)
    with pytest.raises(LexError):
        rewrite_module(b"const s = 'open", url=URL)
    with pytest.raises(InvalidKey):
        rewrite_module(b"import '/x@1.0.0#f';", url=URL)


@pytest.mark.parametrize("media_type", ["text/css; charset=utf-8", "application/wasm", "font/woff2",
                                        "application/json", "application/octet-stream"])
def test_served_body_passes_non_javascript_through(media_type):
    raw = b"\x00\xffimport '/x';@import url(/y.css);"
    assert served_body(raw, media_type, url="https://esm.sh/a@1.0.0/b") == (raw, None)


def test_served_body_rewrites_javascript():
    body, rewrite = served_body(b"import '/x@1.0.0';", "application/javascript; charset=utf-8", url=URL)
    assert body == b"import '../../../x@1.0.0';" and rewrite.imports == ("https://esm.sh/x@1.0.0",)


@pytest.mark.parametrize("media_type, expected", [
    ("application/javascript; charset=utf-8", True), ("text/javascript", True), ("Text/JavaScript", True),
    ("application/x-javascript", True), ("application/json", False), ("text/css", False),
    ("application/typescript", False), ("", False),
])
def test_is_javascript(media_type, expected):
    assert is_javascript(media_type) is expected


@pytest.mark.parametrize("location, current, target", [
    ("/react@19.2.0/es2022/react.mjs", "https://esm.sh/react@19.2.0", "https://esm.sh/react@19.2.0/es2022/react.mjs"),
    ("https://esm.sh/v1/x", "https://esm.sh/x", "https://esm.sh/v1/x"),
    ("y.js", "https://cdn.jsdelivr.net/npm/a@1.0.0/x.js", "https://cdn.jsdelivr.net/npm/a@1.0.0/y.js"),
    ("https://cdn.jsdelivr.net/npm/a@1.0.0/x.js", "https://esm.sh/a", "https://cdn.jsdelivr.net/npm/a@1.0.0/x.js"),
    ("?v=2", "https://esm.sh/a/b", "https://esm.sh/a/b?v=2"),
])
def test_check_redirect_follows_the_allowlist(location, current, target):
    assert check_redirect(location, current) == target


@pytest.mark.parametrize("location", ["https://example.com/x", "http://esm.sh/x", "//evil.test/x", "data:,x"])
def test_check_redirect_refuses_leaving_the_allowlist(location):
    with pytest.raises(InvalidKey):
        check_redirect(location, "https://esm.sh/a")


@pytest.mark.parametrize("url, floating", [
    ("https://esm.sh/react@19.2.0?target=es2022", False),
    ("https://esm.sh/react@19.2.0/es2022/react.mjs", False),
    ("https://esm.sh/@lezer/highlight@^1.0.0?target=es2022", True),
    ("https://esm.sh/prosemirror-view@%5E1.41.3?target=es2022", True),
    ("https://esm.sh/d3@~7.4.0", True),
    ("https://esm.sh/x@latest", True),
    ("https://esm.sh/@embedpdf/models?target=es2022", True),
    ("https://esm.sh/node/process.mjs", True),
    ("https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs/loader.js", False),
    ("https://cdn.jsdelivr.net/npm/a@1.0.0-beta.1/b.js", False),
    ("https://esm.sh/gh/u/r@" + "a" * 40 + "/x.js", True),
    ("https://esm.sh/gh/u/r/" + "a" * 40 + "/x.js", False),
])
def test_is_floating(url, floating):
    assert is_floating(canonical_url(url)) is floating


# -- packages and licenses ------------------------------------------------------------
@pytest.mark.parametrize("url, package", [
    ("https://esm.sh/react@19.2.0/es2022/react.mjs", "react@19.2.0"),
    ("https://esm.sh/@excalidraw/excalidraw@0.18.1/X-abc/es2022/excalidraw.mjs", "@excalidraw/excalidraw@0.18.1"),
    ("https://esm.sh/react@19.2.0?target=es2022", "react@19.2.0"),
    ("https://esm.sh/prosemirror-view@^1.41.3?target=es2022", None),
    ("https://esm.sh/@embedpdf/models?target=es2022", None),
    ("https://esm.sh/node/process.mjs", ESM_SH_POLYFILLS),
    ("https://cdn.jsdelivr.net/npm/katex@0.17.0/dist/fonts/KaTeX_Main-Regular.woff2", "katex@0.17.0"),
    ("https://cdn.jsdelivr.net/npm/@milkdown/crepe@7.21.2/lib/theme/common/ai.css", "@milkdown/crepe@7.21.2"),
])
def test_package_of(url, package):
    assert package_of(url) == package


@pytest.mark.parametrize("expression, allowed", [
    ("MIT", True), ("ISC", True), ("Apache-2.0", True), ("EPL-2.0", True), ("Python-2.0", True),
    ("(MIT OR CC0-1.0)", True), ("(MPL-2.0 OR Apache-2.0)", True), ("MIT OR GPL-3.0", True),
    ("(MIT AND Zlib)", True), ("MIT AND GPL-3.0", False), ("GPL-3.0", False), ("UNKNOWN", False),
    ("SEE LICENSE IN LICENSE", False), ("CC-BY-4.0", False), ("", False), ("(MIT", False), ("MIT OR", False),
])
def test_license_allowed(expression, allowed):
    assert license_allowed(expression) is allowed


# -- roots ------------------------------------------------------------------
def _tree(tmp_path: Path) -> Path:
    """A workspace: one renderer package, the runtime and an empty built-in tree."""
    package = tmp_path / "content/renderers/visual/view"
    package.mkdir(parents=True)
    (package / "view.renderer").write_text("id: view\n")
    (package / "main.tsx").write_text(
        "import React from 'https://esm.sh/react@19.2.0?target=es2022';\n"
        "const V = '1.0.0';\n"
        "const computed = `https://cdn.jsdelivr.net/npm/lib@${V}/x.js`;\n"
        "const prefix = 'https://cdn.jsdelivr.net/npm/lib@1.0.0/dist';\n"
        "const css = \"https://esm.sh/lib@1.0.0/style.css\";\n")
    (package / "main.test.ts").write_text("const t = 'https://esm.sh/only-in-tests@1.0.0';\n")
    (package / "mirror_assets.json").write_text(json.dumps({"assets": [
        {"base": "https://cdn.jsdelivr.net/npm/lib@1.0.0/dist/", "files": ["a.woff2", "sub/b.js"]}]}))
    runtime = tmp_path / "sdk/shared/renderer-runtime"
    runtime.mkdir(parents=True)
    (runtime / "react.js").write_text("import * as R from 'https://esm.sh/react-dom@19.2.0/client?target=es2022';\n")
    (runtime / "session.js").write_text("export const x = 1;\n")
    (tmp_path / "builtins").mkdir()
    return tmp_path


def test_shipped_roots_collects_literals_assets_and_runtime_imports(tmp_path):
    assert shipped_roots(_tree(tmp_path)) == (
        "https://cdn.jsdelivr.net/npm/lib@1.0.0/dist/a.woff2",
        "https://cdn.jsdelivr.net/npm/lib@1.0.0/dist/sub/b.js",
        "https://esm.sh/lib@1.0.0/style.css",
        "https://esm.sh/react-dom@19.2.0/client?target=es2022",
        "https://esm.sh/react@19.2.0?target=es2022",
    )


@pytest.mark.parametrize("document", [
    {"assets": [{"base": "https://cdn.jsdelivr.net/npm/a@1.0.0/dist", "files": ["x"]}]},
    {"assets": [{"base": "https://unpkg.com/a@1.0.0/", "files": ["x"]}]},
    {"assets": [{"base": "https://cdn.jsdelivr.net/npm/a@1.0.0/", "files": []}]},
    {"assets": [{"base": "https://cdn.jsdelivr.net/npm/a@1.0.0/", "files": ["b", "a"]}]},
    {"assets": [{"base": "https://cdn.jsdelivr.net/npm/a@1.0.0/", "files": ["/abs"]}]},
    {"assets": [{"base": "https://cdn.jsdelivr.net/npm/a@1.0.0/", "files": ["../up"]}]},
    {"assets": [{"base": "https://cdn.jsdelivr.net/npm/a@1.0.0/", "files": ["x"], "extra": 1}]},
    {"files": []},
])
def test_mirror_assets_shape_is_enforced(tmp_path, document):
    (tmp_path / "mirror_assets.json").write_text(json.dumps(document))
    with pytest.raises(ValueError):
        read_mirror_assets(tmp_path)


def test_every_shipped_mirror_assets_file_is_valid():
    found = sorted(path for tree in (GENERATORS, RENDERERS, BUILTINS) for path in tree.glob("*/*/mirror_assets.json"))
    assert {path.parent.name for path in found} == {"monaco", "milkdown", "excalidraw"}
    for path in found:
        bases, urls = read_mirror_assets(path.parent)
        assert bases and urls


def test_computed_upstream_urls_ship_mirror_assets():
    """A renderer that builds an upstream URL at runtime (a template with a
    substitution) names the files it can load in mirror_assets.json, or the
    freeze cannot see them."""
    computed = re.compile(r"`(?:" + "|".join(re.escape(u) for u in UPSTREAMS) + r")[^`]*\$\{")
    bad = []
    for tree in (RENDERERS, BUILTINS):
        for source in sorted(tree.glob("*/*/*.ts*")):
            if ".test." in source.name:
                continue
            if computed.search(source.read_text(encoding="utf-8")) and not (source.parent / "mirror_assets.json").is_file():
                bad.append(str(source.relative_to(WORKSPACE)))
    assert not bad, "computed upstream URLs without a mirror_assets.json: " + ", ".join(bad)


def test_mirror_host_lists_are_generated_from_the_contract():
    """The runtime's and the app's host lists and import maps are the
    contract's ``upstreams``, byte for byte (``scripts/sync_mirror_hosts.py``)."""
    script = ROOT / "scripts" / "sync_mirror_hosts.py"
    spec = importlib.util.spec_from_file_location("sync_mirror_hosts", script)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    assert sync.hosts() == HOSTS
    checked = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True, timeout=60)
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_contract_limits():
    assert CONTRACT["max_redirects"] == 5 and CONTRACT["max_module_bytes"] == 32 * 1024 * 1024
    assert CONTRACT["lexer"] == "es-module-lexer@3.0.4/minimal"
    package = json.loads((ROOT / "scripts/package.json").read_text())
    assert package["devDependencies"]["es-module-lexer"] == "3.0.4"
    oracle = (ROOT / "scripts/esm_lexer_oracle.mjs").read_text(encoding="utf-8")
    assert "from 'es-module-lexer/minimal'" in oracle


# -- the freeze and check, against a stubbed upstream -----------------------------
def _graph(tmp_path: Path) -> Path:
    """A workspace whose one renderer imports one esm.sh root."""
    package = tmp_path / "content/renderers/visual/view"
    package.mkdir(parents=True)
    (package / "view.renderer").write_text("id: view\n")
    (package / "main.tsx").write_text("import A from 'https://esm.sh/a@1.0.0?target=es2022';\n")
    runtime = tmp_path / "sdk/shared/renderer-runtime"
    runtime.mkdir(parents=True)
    (runtime / "react.js").write_text("export const x = 1;\n")
    (runtime / "session.js").write_text("export const y = 1;\n")
    (tmp_path / "builtins").mkdir()
    return tmp_path


ROOT_URL = "https://esm.sh/a@1.0.0?target=es2022"
EDGE_URL = "https://esm.sh/b@1.0.0/es2022/b.mjs"


def _upstream(monkeypatch, failing: set[str], served: list[str]):
    from collimate._host import module_mirror

    bodies = {ROOT_URL: b'export * from "/b@1.0.0/es2022/b.mjs";\n', EDGE_URL: b"export const b = 1;\n"}

    def get(url, retries):
        served.append(url)
        if url in failing:
            raise module_mirror._Unavailable("HTTP 500: no space left on device", retry=True)
        return bodies[url], "application/javascript; charset=utf-8", url

    monkeypatch.setattr(module_mirror, "_get", get)
    monkeypatch.setattr(module_mirror, "_registry_metadata", lambda package, retries: {
        "license": "MIT", "dist": {"tarball": f"https://registry.npmjs.org/{package}.tgz"}})


def test_an_upstream_outage_is_a_reported_gap_that_the_next_freeze_closes(tmp_path, monkeypatch):
    from collimate._host.module_mirror import closure_problems, freeze, read_snapshot, snapshot_problems, unavailable_edges
    root = _graph(tmp_path)
    served: list[str] = []
    _upstream(monkeypatch, {EDGE_URL}, served)
    snapshot = freeze(root, rounds=0, log=lambda line: None)
    assert set(snapshot.entries) == {ROOT_URL} and set(snapshot.unavailable) == {EDGE_URL}
    # The graph is closed (the edge is a known gap): check passes and reports it.
    assert closure_problems(snapshot) == []
    assert snapshot_problems(root) == []
    assert unavailable_edges(snapshot) == [
        f"unavailable edge (tolerated): {EDGE_URL}: HTTP 500: no space left on device"]
    # The next freeze keeps what it has and fetches only the gap.
    served.clear()
    _upstream(monkeypatch, set(), served)
    snapshot = freeze(root, rounds=0, log=lambda line: None)
    assert served == [EDGE_URL]
    assert set(snapshot.entries) == {ROOT_URL, EDGE_URL} and not snapshot.unavailable
    assert snapshot_problems(root) == []
    # A refetch that fails keeps the entry it had.
    _upstream(monkeypatch, {ROOT_URL, EDGE_URL}, served)
    snapshot = freeze(root, refresh=True, rounds=0, log=lambda line: None)
    assert set(snapshot.entries) == {ROOT_URL, EDGE_URL} and not snapshot.unavailable
    assert read_snapshot(root / "module-snapshot", verify=True).entries == snapshot.entries


def test_an_unfetchable_root_is_a_hole_the_release_check_refuses(tmp_path, monkeypatch):
    from collimate._host.module_mirror import closure_problems, freeze, snapshot_problems
    root = _graph(tmp_path)
    _upstream(monkeypatch, {ROOT_URL}, [])
    snapshot = freeze(root, rounds=0, log=lambda line: None)
    assert not snapshot.entries and set(snapshot.unavailable) == {ROOT_URL}
    hole = f"root not frozen: {ROOT_URL} (HTTP 500: no space left on device)"
    assert closure_problems(snapshot) == [hole]
    assert snapshot_problems(root) == [hole]


def test_an_unfrozen_root_or_an_unrecorded_edge_is_a_hole():
    from types import MappingProxyType

    from collimate._host.module_mirror import Snapshot, SnapshotEntry, closure_problems, contract_hash, unavailable_edges
    gapped = "https://esm.sh/d@1.0.0?target=es2022"
    entry = SnapshotEntry("0" * 64, "application/javascript", "0" * 64, ROOT_URL,
                          (EDGE_URL, "https://esm.sh/c@1.0.0/es2022/c.mjs"), ())
    snapshot = Snapshot(contract_hash(), (ROOT_URL, gapped),
                        MappingProxyType({ROOT_URL: entry}),
                        MappingProxyType({EDGE_URL: "HTTP 500", gapped: "HTTP 500"}))
    # An unavailable root is a hole even with its reason; an unavailable edge is
    # a tolerated gap check reports.
    assert closure_problems(snapshot) == [
        f"root not frozen: {gapped} (HTTP 500)",
        f"static edge neither frozen nor unavailable: https://esm.sh/c@1.0.0/es2022/c.mjs <- {ROOT_URL}",
    ]
    assert unavailable_edges(snapshot) == [f"unavailable edge (tolerated): {EDGE_URL}: HTTP 500"]


# -- the local esm.sh fallback ----------------------------------------------------
BUILDER = "http://127.0.0.1:18080"
JSDELIVR_URL = "https://cdn.jsdelivr.net/npm/x@1.0.0/x.css"


def _builder_graph(tmp_path: Path) -> Path:
    root = _graph(tmp_path)
    (root / "content/renderers/visual/view/main.tsx").write_text(
        "import A from 'https://esm.sh/a@1.0.0?target=es2022';\n"
        "api.loadCss('https://cdn.jsdelivr.net/npm/x@1.0.0/x.css');\n")
    return root


def _two_upstreams(monkeypatch, public_failing: set[str], calls: list[tuple[str, str | None]]):
    """Public esm.sh answers 500 for ``public_failing``; the local builder
    serves the same keys with its own bodies (root-relative, like esm.sh)."""
    from collimate._host import module_mirror

    public = {ROOT_URL: b'export * from "/b@1.0.0/es2022/b.mjs";\n', EDGE_URL: b"export const b = 1;\n",
              JSDELIVR_URL: b"body{}\n"}
    local = {ROOT_URL: b'export * from "/b@1.0.0/es2022/b.mjs"; // local\n',
             EDGE_URL: b"export const b = 2; // local\n"}

    def get(url, retries, builder=None):
        calls.append((url, builder))
        if builder:
            assert builder == BUILDER and url.startswith("https://esm.sh/")
            return local[url], "application/javascript; charset=utf-8", url
        if url in public_failing:
            raise module_mirror._Unavailable("HTTP 500: no space left on device", retry=True, status=500)
        return public[url], ("text/css" if url.endswith(".css") else "application/javascript"), url

    monkeypatch.setattr(module_mirror, "_get", get)
    monkeypatch.setattr(module_mirror, "check_esm_builder", lambda builder: None)
    monkeypatch.setattr(module_mirror, "_registry_metadata", lambda package, retries: {
        "license": "MIT", "dist": {"tarball": f"https://registry.npmjs.org/{package}.tgz"}})


def test_a_cold_build_public_esm_sh_refuses_comes_from_the_local_builder_under_its_public_key(tmp_path, monkeypatch):
    from collimate._host.module_mirror import BUILDERS_FILE, freeze, read_builders, snapshot_problems
    root = _builder_graph(tmp_path)
    calls: list[tuple[str, str | None]] = []
    _two_upstreams(monkeypatch, {ROOT_URL}, calls)
    snapshot = freeze(root, rounds=0, esm_builder=BUILDER, log=lambda line: None)
    # The key and final URL stay public; the body is the builder's, rewritten as ever.
    assert set(snapshot.entries) == {ROOT_URL, EDGE_URL, JSDELIVR_URL} and not snapshot.unavailable
    assert snapshot.entries[ROOT_URL].final_url == ROOT_URL
    assert snapshot.entries[ROOT_URL].imports == (EDGE_URL,)
    body = (root / "module-snapshot/modules" / snapshot.entries[ROOT_URL].hash).read_bytes()
    assert body == served_body(b'export * from "/b@1.0.0/es2022/b.mjs"; // local\n', "application/javascript",
                               url=ROOT_URL)[0]
    assert body.endswith(b"// local\n") and b'"/b@' not in body
    # The whole not-yet-frozen subgraph comes from that builder; jsDelivr never does.
    assert (EDGE_URL, BUILDER) in calls and (EDGE_URL, None) not in calls
    assert all(builder is None for url, builder in calls if url == JSDELIVR_URL)
    assert dict(read_builders(root / "module-snapshot")) == {ROOT_URL: "local", EDGE_URL: "local"}
    record = json.loads((root / "module-snapshot" / BUILDERS_FILE).read_text())
    assert record["image"] == CONTRACT["esm_builder_image"] and record["schema"] == 1
    assert snapshot_problems(root) == []
    # A later freeze keeps the local entries and their mark without a builder;
    # a refresh that fetches them publicly drops it.
    calls.clear()
    _two_upstreams(monkeypatch, set(), calls)
    freeze(root, rounds=0, log=lambda line: None)
    assert calls == [] and set(read_builders(root / "module-snapshot")) == {ROOT_URL, EDGE_URL}
    freeze(root, refresh=True, rounds=0, log=lambda line: None)
    assert dict(read_builders(root / "module-snapshot")) == {}
    assert snapshot_problems(root) == []


def test_a_frozen_shared_key_stays_when_a_family_falls_back(tmp_path, monkeypatch):
    from collimate._host.module_mirror import freeze, read_builders
    root = _builder_graph(tmp_path)
    calls: list[tuple[str, str | None]] = []
    _two_upstreams(monkeypatch, set(), calls)
    freeze(root, rounds=0, log=lambda line: None)
    # The root's public build now fails; its frozen edge is shared and stays.
    (root / "content/renderers/visual/view/main.tsx").write_text(
        "import A from 'https://esm.sh/a@1.0.0?target=es2022&v=2';\n"
        "api.loadCss('https://cdn.jsdelivr.net/npm/x@1.0.0/x.css');\n")
    other = "https://esm.sh/a@1.0.0?target=es2022&v=2"
    calls.clear()
    from collimate._host import module_mirror

    def get(url, retries, builder=None):
        calls.append((url, builder))
        if builder:
            return b'export * from "/b@1.0.0/es2022/b.mjs"; // local\n', "application/javascript", url
        raise module_mirror._Unavailable("HTTP 502", retry=True, status=502)

    monkeypatch.setattr(module_mirror, "_get", get)
    snapshot = freeze(root, rounds=0, esm_builder=BUILDER, log=lambda line: None)
    assert calls == [(other, None), (other, BUILDER)]
    assert other in snapshot.entries and EDGE_URL in snapshot.entries
    assert dict(read_builders(root / "module-snapshot")) == {other: "local"}


def test_a_4xx_or_a_jsdelivr_failure_never_falls_back(tmp_path, monkeypatch):
    from collimate._host import module_mirror
    root = _builder_graph(tmp_path)
    calls: list[tuple[str, str | None]] = []

    def get(url, retries, builder=None):
        calls.append((url, builder))
        if url == ROOT_URL:
            raise module_mirror._Unavailable("HTTP 404", retry=False, status=404)
        raise module_mirror._Unavailable("HTTP 503", retry=True, status=503)

    monkeypatch.setattr(module_mirror, "_get", get)
    monkeypatch.setattr(module_mirror, "check_esm_builder", lambda builder: None)
    snapshot = module_mirror.freeze(root, rounds=0, esm_builder=BUILDER, log=lambda line: None)
    assert calls == [(JSDELIVR_URL, None), (ROOT_URL, None)] or calls == [(ROOT_URL, None), (JSDELIVR_URL, None)]
    assert set(snapshot.unavailable) == {ROOT_URL, JSDELIVR_URL}


def test_builder_redirects_map_back_onto_the_public_host():
    from collimate._host.module_mirror import _builder_url, _from_builder
    assert _builder_url("https://esm.sh/react@19.2.0?target=es2022", BUILDER + "/") == (
        BUILDER + "/react@19.2.0?target=es2022")
    assert _from_builder(BUILDER + "/react@19.2.0/es2022/react.mjs", BUILDER) == "https://esm.sh/react@19.2.0/es2022/react.mjs"
    assert _from_builder("/react@19.2.0/es2022/react.mjs", BUILDER) == "https://esm.sh/react@19.2.0/es2022/react.mjs"
    with pytest.raises(ValueError):
        _builder_url(JSDELIVR_URL, BUILDER)


def test_a_builder_of_another_release_is_refused(monkeypatch):
    import io

    from collimate._host import module_mirror
    monkeypatch.setattr(module_mirror.urllib.request, "urlopen",
                        lambda url, timeout: io.BytesIO(b'{"version":"v139"}'))
    with pytest.raises(SystemExit, match="v139"):
        module_mirror.check_esm_builder(BUILDER)
    monkeypatch.setattr(module_mirror.urllib.request, "urlopen",
                        lambda url, timeout: io.BytesIO(json.dumps({"version": module_mirror.builder_tag()}).encode()))
    module_mirror.check_esm_builder(BUILDER)


def test_builders_json_must_name_esm_sh_entries_of_the_snapshot(tmp_path, monkeypatch):
    from collimate._host.module_mirror import BUILDERS_FILE, freeze, snapshot_problems
    root = _builder_graph(tmp_path)
    _two_upstreams(monkeypatch, set(), [])
    freeze(root, rounds=0, log=lambda line: None)
    path = root / "module-snapshot" / BUILDERS_FILE
    path.write_text(json.dumps({"schema": 1, "image": CONTRACT["esm_builder_image"],
                                "entries": {JSDELIVR_URL: "local", "https://esm.sh/z@1.0.0": "local"}}))
    assert snapshot_problems(root) == [
        f"{BUILDERS_FILE} names a key the snapshot does not hold as an esm.sh entry: {JSDELIVR_URL}",
        f"{BUILDERS_FILE} names a key the snapshot does not hold as an esm.sh entry: https://esm.sh/z@1.0.0"]
    path.write_text(json.dumps({"schema": 1, "image": "ghcr.io/esm-dev/esm.sh:v1", "entries": {}}))
    assert snapshot_problems(root) == [
        f"{BUILDERS_FILE} names ghcr.io/esm-dev/esm.sh:v1, not the contract's {CONTRACT['esm_builder_image']}"]
