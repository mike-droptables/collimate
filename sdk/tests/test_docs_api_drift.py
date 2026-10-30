# SPDX-License-Identifier: MIT
"""Guard against API drift in the canonical authoring docs.

The how-to guides ship verbatim as the Guide's skill references, so a
wrong ``gen.*`` reference there is prompt-poisoning, not just a doc typo.
This test asserts every ``gen.<name>`` mentioned in the canonical docs
resolves to a real public symbol — either ``collimate.native.__all__`` or a
module-level dynamic attribute — that every ``gen.<name>(...)`` call written
out in them parses and passes arguments the real signature accepts, that
every ``gen.annotations.<member>`` they name (and call) is a real member of
the annotation namespace, that every ``gen.session.<name>`` is one of the
session machinery's exports (and binds to its signature), that
every ``collimate.<module>.<name>`` they name exists in a public module, and
that the API reference names every public ``gen`` export. The first-party
definitions' ``summary:`` lines are held to the same names.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import re
from pathlib import Path

import pytest

import collimate
from collimate import native

from tests._workspace import BUILTINS, CONTENT, GENERATORS, WORKSPACE


_REPO = Path(__file__).resolve().parents[1]

# Resolved via PEP 562 module ``__getattr__`` in collimate.native, so they
# aren't in ``__all__`` but are valid ``gen.*`` references.
_DYNAMIC_ATTRS = set(native.DYNAMIC_ATTRS)

# ``gen.<identifier>`` (attribute access, with or without a following call).
# Catches gen.update_cell / gen.start_terminal / gen.fs(.write) drift while
# leaving gen.history.append (history is public) and gen.prompt alone.
_GEN_REF = re.compile(r"\bgen\.([A-Za-z_]\w*)")


def _allowed() -> set[str]:
    # ``gen.session`` is the Agent generator's session namespace, checked
    # member by member below.
    return set(native.__all__) | _DYNAMIC_ATTRS | {"session"}


def _canonical_docs() -> list[Path]:
    docs = sorted((_REPO / "shared" / "docs").glob("*.md"))
    docs += [_REPO / "README.md", GENERATORS / "README.md"]
    return docs


def _definition_summaries() -> list[tuple[str, str]]:
    """``(where, summary)`` of every first-party definition: the catalog line
    a user or an agent reads about it."""
    import yaml
    found = []
    for path in sorted(p for root in (BUILTINS, CONTENT) for p in root.rglob("*")
                       if p.suffix in (".renderer", ".generator", ".handler", ".runner")):
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("summary"), str):
            found.append((f"{path.relative_to(WORKSPACE)}: summary", value["summary"]))
    return found


def _scanned() -> list[tuple[str, str]]:
    """``(where, text)`` of each canonical doc and each definition summary."""
    return [(str(doc.relative_to(WORKSPACE)), doc.read_text(encoding="utf-8"))
            for doc in _canonical_docs()] + _definition_summaries()


def test_the_scan_reads_both_trees_definition_summaries():
    trees = {where.split("/", 1)[0] for where, _ in _definition_summaries()}
    assert trees == {"builtins", "content"}


def test_canonical_docs_only_reference_public_gen_api():
    allowed = _allowed()
    violations: dict[str, set[str]] = {}
    for where, text in _scanned():
        for m in _GEN_REF.finditer(text):
            name = m.group(1)
            if name not in allowed:
                violations.setdefault(where, set()).add(name)
    assert not violations, (
        "Canonical docs reference gen.* names not in collimate.native.__all__ "
        "(these docs are shipped as Guide references, so they must "
        f"match the real API): { {k: sorted(v) for k, v in violations.items()} }"
    )


_CALL_START = re.compile(r"\bgen\.([A-Za-z_]\w*)\(")


def _normalise_placeholders(text: str) -> str:
    """The docs' shorthand as Python: ``…`` is ``...``, a keyword written
    without a value (``name=,``) takes ``...``, and a trailing ``, ...``
    (further arguments elided) is dropped."""
    text = text.replace("…", "...")
    text = re.sub(r"=\s*(?=[,)])", "=...", text)
    return re.sub(r",\s*\.\.\.\s*\)", ")", text)


def _gen_calls(text: str, pattern: "re.Pattern" = _CALL_START):
    """``(name, source, ast.Call | None)`` for every ``gen.<name>(...)`` call
    (or every call ``pattern`` finds) written out in ``text``; the node is
    ``None`` when the call does not parse as Python even with the shorthand
    normalised (a drift of its own)."""
    text = _normalise_placeholders(text)
    for m in pattern.finditer(text):
        depth, end = 0, None
        for j in range(m.end() - 1, len(text)):
            if text[j] in "([{":
                depth += 1
            elif text[j] in ")]}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        source = text[m.start():end] if end is not None else text[m.start():m.end() + 40]
        try:
            node = ast.parse(source, mode="eval").body
        except SyntaxError:
            node = None
        yield m.group(1), source, node if isinstance(node, ast.Call) else None


def _call_problem(fn, call: ast.Call) -> "str | None":
    """Why ``call`` could not bind to ``fn``'s signature, or None."""
    params = list(inspect.signature(fn).parameters.values())
    kinds = {p.kind for p in params}
    if inspect.Parameter.VAR_KEYWORD not in kinds:
        named = {p.name for p in params
                 if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
        unknown = [k.arg for k in call.keywords if k.arg and k.arg not in named]
        if unknown:
            return f"unknown keyword(s) {unknown}"
    if inspect.Parameter.VAR_POSITIONAL not in kinds and not any(
            isinstance(a, ast.Starred) for a in call.args):
        positional = [p for p in params
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        if len(call.args) > len(positional):
            return f"{len(call.args)} positional arguments, at most {len(positional)}"
    return None


def test_canonical_doc_calls_bind_to_the_real_signatures():
    violations: list = []
    for where, text in _scanned():
        for name, source, call in _gen_calls(text):
            if call is None:
                violations.append(f"{where}: {source} — does not parse")
                continue
            fn = getattr(native, name, None)
            if not callable(fn):
                continue
            problem = _call_problem(fn, call)
            if problem:
                violations.append(f"{where}: {source} — {problem}")
    assert not violations, (
        "Canonical docs call gen.* with arguments the API rejects:\n" + "\n".join(violations))


@pytest.mark.parametrize("snippet, problem", [
    ('gen.commit(prompt="hi", config={})', "unknown keyword(s) ['prompt', 'config']"),
    ('gen.read_file("r", "cell")', "2 positional arguments, at most 1"),
    ("gen.update_file(name, content=...)", None),
    ("gen.ask('Run it?', kind='execute', detail='ls')", None),
    ("gen.ask(title, kind=, detail=)", None),                   # shorthand keywords
    ("gen.update_file(name, content=..., …)", None),        # elided arguments
    ("gen.commit(nme=)", "unknown keyword(s) ['nme']"),
    ("gen.commit(name=, pinned=)", "unknown keyword(s) ['pinned']"),
])
def test_the_call_check_reads_keywords_and_positionals(snippet, problem):
    [(name, _, call)] = list(_gen_calls(snippet))
    assert _call_problem(getattr(native, name), call) == problem


def test_a_call_that_does_not_parse_is_reported_not_skipped():
    [(_, _, call)] = list(_gen_calls("gen.commit(name=[, x=)"))
    assert call is None


# ``gen.annotations.<member>`` and its calls: the namespace object's public
# members, checked on its class so a property is not evaluated.
_ANNOTATIONS_REF = re.compile(r"\bgen\.annotations\.([A-Za-z_]\w*)")
_ANNOTATIONS_CALL = re.compile(r"\bgen\.annotations\.([A-Za-z_]\w*)\(")


def _annotation_problems(text: str) -> list:
    namespace = type(native.annotations)
    problems: list = []
    for m in _ANNOTATIONS_REF.finditer(text):
        name = m.group(1)
        try:
            inspect.getattr_static(namespace, name)
        except AttributeError:
            problems.append(f"gen.annotations.{name} is not a member")
            continue
        if name.startswith("_"):
            problems.append(f"gen.annotations.{name} is private")
    for name, source, call in _gen_calls(text, _ANNOTATIONS_CALL):
        if call is None:
            problems.append(f"{source} — does not parse")
            continue
        member = inspect.getattr_static(namespace, name, None)
        if not inspect.isfunction(member):
            continue
        problem = _call_problem(getattr(native.annotations, name), call)
        if problem:
            problems.append(f"{source} — {problem}")
    return problems


_SESSION_REF = re.compile(r"\bgen\.session\.([A-Za-z_]\w*)")
_SESSION_CALL = re.compile(r"\bgen\.session\.([A-Za-z_]\w*)\(")


def _session_problems(text: str) -> list:
    import gen.session as session
    problems = [f"gen.session.{m.group(1)} is not exported" for m in _SESSION_REF.finditer(text)
                if m.group(1) not in session.__all__]
    for name, source, call in _gen_calls(text, _SESSION_CALL):
        if call is None:
            problems.append(f"{source} — does not parse")
        elif name in session.__all__:
            problem = _call_problem(getattr(session, name), call)
            if problem:
                problems.append(f"{source} — {problem}")
    return problems


def test_canonical_docs_name_real_session_members():
    violations = {where: _session_problems(text) for where, text in _scanned()}
    violations = {k: v for k, v in violations.items() if v}
    assert not violations, violations


@pytest.mark.parametrize("text, problems", [
    ("`gen.session.submit_turn(text, source=...)`", []),
    ("`gen.session.serve_chat`", ["gen.session.serve_chat is not exported"]),
    ("gen.session.interrupt_turn(now=True)", ["gen.session.interrupt_turn(now=True) — unknown keyword(s) ['now']"]),
])
def test_the_session_member_check_catches_drift(text, problems):
    assert _session_problems(text) == problems


def test_canonical_docs_name_real_annotation_members():
    violations = {where: _annotation_problems(text) for where, text in _scanned()}
    violations = {k: v for k, v in violations.items() if v}
    assert not violations, violations


@pytest.mark.parametrize("text, problems", [
    ("await gen.annotations.read()", ["gen.annotations.read is not a member"]),
    ("await gen.annotations.post(text, reply=)",
     ["gen.annotations.post(text, reply=...) — unknown keyword(s) ['reply']"]),
    ("gen.annotations.subscribe(cb, on_interrupt=)", []),
    ("gen.annotations.notice and gen.annotations.available", []),
])
def test_the_annotation_member_check_catches_drift(text, problems):
    assert _annotation_problems(text) == problems


# ``collimate.<module>[.<name>…]`` as code, not inside a URL, an email address or a string key.
_COLLIMATE_REF = re.compile(r"(?<![\w/.'\"@-])collimate\.([A-Za-z_]\w*)((?:\.[A-Za-z_]\w*)*)")
_FROM_IMPORT = re.compile(r"\bfrom collimate\.([A-Za-z_]\w*) import ([A-Za-z_][\w, ]*)")


def _collimate_ref_problems(text: str) -> list:
    problems: list = []
    refs = [(m.group(1), [p for p in m.group(2).split(".") if p],
             bool(re.search(r"\bpython(?:3)?\s+-m\s+$", text[:m.start()])))
            for m in _COLLIMATE_REF.finditer(text)]
    refs += [(m.group(1), [n.strip()], False) for m in _FROM_IMPORT.finditer(text)
             for n in m.group(2).split(",") if n.strip()]
    for module, names, cli in refs:
        if module not in collimate.__all__ and not cli:
            problems.append(f"collimate.{module} is not a public module")
            continue
        try:
            obj = importlib.import_module(f"collimate.{module}")
        except ImportError:
            problems.append(f"collimate.{module} does not exist")
            continue
        for i, name in enumerate(names):
            if hasattr(obj, name):
                obj = getattr(obj, name)
                continue
            try:                      # a submodule (``python -m collimate.x.y``)
                obj = importlib.import_module(f"{obj.__name__}.{name}")
            except (AttributeError, ImportError):
                problems.append(f"collimate.{module}.{'.'.join(names[:i + 1])} does not exist")
                break
    return problems


def test_canonical_docs_name_only_real_public_collimate_symbols():
    violations = {where: _collimate_ref_problems(text) for where, text in _scanned()}
    violations = {k: v for k, v in violations.items() if v}
    assert not violations, violations


@pytest.mark.parametrize("text, problems", [
    ("`collimate.permissions.effective_commands(grant, can_ask)`", []),
    ("`from collimate.pointers import website_pointer, terminal_pointer`", []),
    ("[Why Collimate](why-collimate.md)", []),
    ("`collimate.permissions.commands_level`", ["collimate.permissions.commands_level does not exist"]),
    ("`collimate.native.grant`", ["collimate.native is not a public module"]),
    ("`python -m collimate.renderer_runtime --out DIR`", []),
    ("`collimate.renderer_runtime`", ["collimate.renderer_runtime is not a public module"]),
    ("`python -m collimate.missing_cli`", ["collimate.missing_cli does not exist"]),
    ("`from collimate.fs import _open_ws_dir`", ["collimate.fs._open_ws_dir does not exist"]),
    ("see https://collimate.dev and localStorage['collimate.html.x']", []),
    ("write to <michael@collimate.dev>", []),
])
def test_the_collimate_reference_check(text, problems):
    assert _collimate_ref_problems(text) == problems


def test_the_api_reference_names_every_public_gen_export():
    """Every name ``gen`` exports is documented in the API reference (as a
    row, in prose, or among the internal session verbs)."""
    reference = (_REPO / "shared" / "docs" / "api-reference.md").read_text(encoding="utf-8")
    missing = [n for n in native.__all__
               if not re.search(r"(?<![\w.])(gen\.)?" + re.escape(n) + r"\b", reference)]
    assert not missing, f"api-reference.md does not mention: {missing}"


# Renderer contracts are TypeScript; parse the interface's outer members, not
# nested callback fields or prose. Public hooks include the platform declarations.
def _renderer_members():
    source = (_REPO / "packages/renderer-hooks/src/types.ts").read_text()
    body = source.split("export interface RendererAPI {", 1)[1].split("\n}", 1)[0]
    return set(re.findall(r"^    ([A-Za-z_]\w*)\??[(:]", body, re.M))


def _renderer_refs(text):
    # Ignore URLs such as https://api.example.com; api.member in code/prose is
    # always checked, including optional calls and member references.
    return set(re.findall(r"(?<![\w/.-])api\.([A-Za-z_]\w*)", text))


def test_renderer_api_members_match_public_types__F1894():
    allowed = _renderer_members()
    violations = {where: sorted(_renderer_refs(text) - allowed) for where, text in _scanned()}
    assert not {p: names for p, names in violations.items() if names}, violations


def test_renderer_hook_table_matches_exports__F1894():
    package = _REPO / "packages/renderer-hooks"
    exports = set()
    for source in (package / "src/index.tsx", package / "platform.d.ts"):
        exports.update(re.findall(r"^export (?:function|const) (\w+)", source.read_text(), re.M))
    guide = (_REPO / "shared/docs/how-to-write-a-renderer.md").read_text()
    table = guide.split("| Hook |", 1)[1].split("`api` (the", 1)[0]
    labels = "\n".join(line.split("|", 2)[1] for line in table.splitlines() if line.startswith("|"))
    names = set(re.findall(r"`(\w+)(?:\(|`)", labels))
    assert names <= exports, f"unknown documented hooks: {sorted(names - exports)}"
    # Completeness matters too: a new hook must appear in the quick reference.
    assert exports <= names, f"undocumented exported hooks: {sorted(exports - names)}"


def _manifest_yaml_blocks(doc):
    import yaml
    for block in re.findall(r"^```ya?ml\b[^\n]*\n(.*?)^```", doc.read_text(), re.M | re.S):
        data = yaml.safe_load(block)  # Invalid examples fail instead of disappearing.
        if not isinstance(data, dict):
            continue  # A set_yaml field list is not a manifest.
        if doc.name == "how-to-write-a-generator.md":
            if "options" in data:
                from collimate.options_stack import parse_options_stack
                parse_options_stack('.options_stack', block)
                continue  # Separate options declaration schema, checked above.
            if "fields" in data or "# configs/" in block:
                continue  # main settings schema / named configuration recipe
        yield data


def test_documented_manifest_keys_match_lint__F1894():
    from collimate.lint import GENERATOR_FIELDS, RENDERER_FIELDS, RUNNER_FIELDS
    schemas = {"generator": GENERATOR_FIELDS, "renderer": RENDERER_FIELDS, "runner": RUNNER_FIELDS}
    for kind, fields in schemas.items():
        doc = _REPO / "shared/docs" / f"how-to-write-a-{kind}.md"
        blocks = list(_manifest_yaml_blocks(doc))
        assert blocks, f"no {kind} manifest examples checked"
        for data in blocks:
            assert data.keys() <= fields, f"{doc.name}: unknown keys {sorted(data.keys() - fields)}"
    assert "timeout_seconds" in GENERATOR_FIELDS
    assert "`timeout_seconds`" in (_REPO / "shared/docs/how-to-write-a-generator.md").read_text()


@pytest.mark.parametrize("text, invalid", [
    ("api.getSiblingFile('a')", set()),
    ("api.getSiblingFiles('a')", {"getSiblingFiles"}),
    ("api.onChangeTypo?.(f)", {"onChangeTypo"}),
    ("https://api.example.com/path", set()),
])
def test_renderer_reference_check_catches_drift__F1894(text, invalid):
    assert _renderer_refs(text) - _renderer_members() == invalid


def test_renderer_guide_package_scope_matches_real_discovery__P0F05():
    """A nested definition is content, including below a different kind."""
    from collimate.definitions import discover_definitions, package_files

    guide = (_REPO / 'shared/docs/how-to-write-a-renderer.md').read_text()
    scope = next((paragraph for paragraph in guide.split('\n\n')
                  if 'Stack discovery takes' in paragraph), '')
    assert 'shallowest definitions' in scope
    assert 'a nested definition is not discovered as a second package' in scope
    assert 'exactly one definition file across all four kinds' in scope
    assert 'Entrypoints resolve relative to their package folder' in scope

    paths = [
        'docs/card/view.renderer', 'docs/card/main.tsx',
        'docs/card/examples/nested.renderer',
        'worker/job.generator', 'worker/sample/view.renderer',
        'mixed/a.renderer', 'mixed/b.handler', 'mixed/nested/view.renderer',
        '_runs/completed/view.renderer', 'root.renderer',
    ]
    found = discover_definitions(paths)
    assert sorted((package.kind, package.definition_path) for package in found.packages) == [
        ('generator', 'worker/job.generator'), ('renderer', 'docs/card/view.renderer'),
    ]
    assert [(problem.code, problem.path) for problem in found.problems] == [
        ('definition-folder-ambiguous', 'mixed'), ('definition-at-root', 'root.renderer'),
    ]
    card = next(package for package in found.packages if package.prefix == 'docs/card')
    assert package_files(card, paths) == (
        'docs/card/examples/nested.renderer', 'docs/card/main.tsx', 'docs/card/view.renderer',
    )
