# SPDX-License-Identifier: MIT
"""collimate._host.esm_lexer: the import scan the module mirror rewrites with.

Hand-picked cases for each state the port carries (strings, templates,
comments, the regex/division heuristics, the statement forms). The
whole-corpus comparison with es-module-lexer itself is
``test_esm_lexer_oracle.py``.
"""
from __future__ import annotations

import pytest

from collimate._host.esm_lexer import LexError, scan_imports


def names(source: str) -> list[tuple[str | None, bool]]:
    return [(record.name, record.dynamic) for record in scan_imports(source)]


def test_static_import_span_is_the_text_inside_the_quotes():
    source = 'import a from "./a.js";\nimport {b as c} from \'b\';\nimport "side";'
    records = scan_imports(source)
    assert [(r.name, r.dynamic) for r in records] == [("./a.js", False), ("b", False), ("side", False)]
    assert [source[r.start:r.end] for r in records] == ["./a.js", "b", "side"]


def test_minified_imports_and_reexports():
    source = ('import{TemplateResult as t}from"lit-html";import{a as e}from"./chunk.js";'
              'export{j as SVG,i as T}from"./chunk.js";export*from"/x.mjs";export*as ns from"m"')
    assert names(source) == [("lit-html", False), ("./chunk.js", False), ("./chunk.js", False),
                             ("/x.mjs", False), ("m", False)]


def test_string_escapes_decode_and_the_span_keeps_the_raw_text():
    source = "import './\\x61\\u{62}c.js';\nimport('./\\u0064.js');"
    records = scan_imports(source)
    assert [r.name for r in records] == ["./abc.js", "./d.js"]
    assert [source[r.start:r.end] for r in records] == ["./\\x61\\u{62}c.js", "./\\u0064.js"]


def test_import_attributes():
    source = "import j from './a.json' with { type: 'json' };\nimport('./b.json', { with: { type: 'json' } });"
    assert names(source) == [("./a.json", False), ("./b.json", True)]


def test_dynamic_imports_literal_template_and_computed():
    source = "import('a'); import(`./t.js`); import(`./${x}.js`); import(name); import('p' + q);"
    records = scan_imports(source)
    assert [(r.name, r.dynamic) for r in records] == [
        ("a", True), ("./t.js", True), (None, True), (None, True), (None, True)]
    assert source[records[1].start:records[1].end] == "./t.js"


def test_nested_templates_and_their_imports():
    source = "const s = `a${`b${import('inner')}c`}d`; import('after');"
    assert names(source) == [("inner", True), ("after", True)]


def test_import_meta_is_not_an_import():
    assert names("console.log(import.meta.url); import(import.meta.resolve('x'));") == [(None, True)]
    assert names("export var hello = 'world';\nconsole.log(import.meta.url);") == []


def test_member_and_method_named_import_are_not_imports():
    assert names("obj.import('x'); a.\n// comment\nimport('y');") == []
    assert names("class C { import(a, b) {} static import() {} }") == []
    assert names("const o = { import(a, b) {} };") == []
    assert names("({ import: () => import('real') })") == [("real", True)]


def test_division_is_not_a_regex():
    assert names("const r = a / b / c; import('after');") == [("after", True)]
    assert names("x = 5. / 10; import('after');") == [("after", True)]
    assert names("tick++/fetti; import('after');") == [("after", True)]
    assert names("const x = num / /'/.exec(l)[0]; import('after');") == [("after", True)]
    assert names("a / 'b' / c; import('after');") == [("after", True)]
    assert names("function variance(){return s/(a-1)}; import('after');") == [("after", True)]


def test_regex_after_paren_keywords_braces_and_value_keywords():
    # Each regex body holds a quote or an import that would derail a lexer
    # treating its '/' as division.
    assert names("if (a) /x'import('no')/.test(b); import('after');") == [("after", True)]
    assert names("while (true) /'/.test(b); import('after');") == [("after", True)]
    assert names("try {} finally {} /a'/g; import('after');") == [("after", True)]
    assert names("x();{f()}/a'/g; import('after');") == [("after", True)]
    assert names("function f() { return /'import('no')/; } import('after');") == [("after", True)]
    assert names("x = typeof /'/; y = void /'/; import('after');") == [("after", True)]
    assert names("for (const t of /a'[0-9]+/g.exec(s)) {} import('after');") == [("after", True)]
    assert names("x: while (1) {\n  if (a) break\n  /import('no')/.test(b)\n}\nimport('after');") == [("after", True)]


def test_regex_character_class_holds_a_slash():
    assert names("const re = /[/'\"]/g; import('after');") == [("after", True)]


def test_comments_hide_imports():
    source = "// import('no')\n/* import('no') */ import 'yes'; /**/ import('dyn') // tail"
    assert names(source) == [("yes", False), ("dyn", True)]


def test_strings_hide_imports():
    assert names("const s = \"import('no')\"; const t = 'import(\"no\")'; import('yes')") == [("yes", True)]


def test_non_breaking_space_and_unicode_identifiers():
    assert names(" import('nbsp');") == [("nbsp", True)]
    assert names("export const a = π / 2, b = 3; import('after');") == [("after", True)]


def test_exports_with_initializers_surface_their_dynamic_imports():
    source = "export const x = import('a'), y = 2;\nexport const { p = import('c') } = o;"
    assert names(source) == [("a", True), ("c", True)]


def test_positions_count_code_points_after_astral_characters():
    source = "const e = '😀'; import('x');"
    (record,) = scan_imports(source)
    assert source[record.start:record.end] == "x"


@pytest.mark.parametrize("source", [
    "const s = 'unterminated",
    "const t = `unterminated",
    "x = a(/unterminated\n)",
    "import('x'",
    "f())",
    "})",
    "import {a} from x",
])
def test_unlexable_input_raises(source):
    with pytest.raises(LexError) as raised:
        scan_imports(source)
    assert isinstance(raised.value.position, int)


@pytest.mark.parametrize(("source", "position"), [
    ('import a from "\\x4";', 15),  # a static specifier's error is inside its quotes
    ("import('\\u{110000}');", 7),  # a literal dynamic one's at its quote
    ("import '\\08';", 8),  # legacy octal escapes do not decode
    ("import(`\\1`);", 7),
    ('export { "\\x4" as y };', 9),  # nor does a quoted export name
    ("export * as '\\q\\9' from 'm';", 12),
    ('import "a"; export { "\\x" as b }; import "\\9";', 42),  # specifiers decode first
])
def test_an_escape_that_does_not_decode_is_a_parse_error(source, position):
    with pytest.raises(LexError) as raised:
        scan_imports(source)
    assert raised.value.position == position


def test_a_reexported_local_name_is_not_decoded():
    assert names('export { "\\u{}" as b } from "m";') == [("m", False)]


def test_template_specifier_line_endings_normalise():
    assert names("import(`c\r\nd`); import(`e\\\r\nf`);") == [("c\nd", True), ("ef", True)]


def test_nesting_deeper_than_the_token_stacks_is_a_parse_error():
    assert scan_imports("[" * 1024 + "]" * 1024) == ()
    assert scan_imports("`${" * 512 + "}`" * 512) == ()
    assert len(scan_imports("import(" * 512 + "x" + ")" * 512)) == 512
    for source, position in (("(" * 1025 + ")" * 1025, 1024), ("{" * 1025 + "}" * 1025, 1024),
                             ("`${" * 513 + "}`" * 513, 1536), ("import(" * 513 + "x" + ")" * 513, 3590)):
        with pytest.raises(LexError) as raised:
            scan_imports(source)
        assert raised.value.position == position


def test_import_attributes_after_a_line_break_with_a_trailing_comma():
    assert names("import x from 'a'\n  with { type : 'json', }\nimport('b')") == [("a", False), ("b", True)]


def test_leniency_matches_es_module_lexer():
    # It tracks nesting depth, not bracket kinds; a '/' after an object
    # literal's '}' is division, so a quote after it opens a string; and the
    # scan starts at a statement boundary, so after a leading block's '}' a
    # '/' opens a regular expression.
    assert scan_imports("f(]") == ()
    with pytest.raises(LexError):
        scan_imports("x = {} /a'/g; import('after');")
    assert [record.name for record in scan_imports("{} /a'/g; import('after');")] == ["after"]


def test_empty_and_trivial_sources():
    assert scan_imports("") == ()
    for source in ("export { x }", "'asdf'", "/asdf/", "`asdf`", "/**/", "//"):
        assert scan_imports(source) == ()
