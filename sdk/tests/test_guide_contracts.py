"""The guides' examples, each run or matched by the test bound to its label.

A block a test reads is labelled in its fence info string (```` ```yaml
example=<label> ````; a table, by an ``<!-- example=<label> -->`` line above it).
``@binds(label, ...)`` runs the test once per label, handed that block's text.
"""

from __future__ import annotations

import ast
import functools
import json
from pathlib import Path
import re
import time

import pytest
import yaml

from tests._workspace import BUILTINS, RENDERERS, WORKSPACE


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "shared/docs"
GUIDES = ("how-to-write-a-renderer.md", "how-to-write-a-handler.md", "how-to-write-options.md")
FENCE = re.compile(r"^```(\w+)([^\n]*)\n(.*?)^```[ \t]*$", re.M | re.S)
TABLE = re.compile(r"^<!-- example=([\w-]+) -->\n((?:\|[^\n]*\n)+)", re.M)
LABEL = re.compile(r"\bexample=([\w-]+)")
MODULE_LANGUAGES = ("tsx", "jsx", "typescript", "ts")


def blocks(guide):
    """``(language, info, body)`` of every top-level fence in ``guide``."""
    return FENCE.findall((DOCS / guide).read_text())


@functools.cache
def examples():
    """``{label: text}`` of every labelled block in the guides."""
    found = {}
    for guide in GUIDES:
        labelled = [(LABEL.search(info), body) for _, info, body in blocks(guide)]
        labelled = [(label.group(1), body) for label, body in labelled if label]
        for label, body in labelled + TABLE.findall((DOCS / guide).read_text()):
            assert label not in found, f"two guide blocks are labelled {label}"
            found[label] = body
    return found


BOUND: dict[str, list[str]] = {}
# The upload readback runs against the app's file routes, so the app's suite runs it.
APP_BOUND = {
    "renderer-upload-readback": (
        "api/tests/test_pytest_file_blob_hash.py",
        "test_documented_staged_upload_is_readable_before_history_commit__D12",
    )
}


def binds(*labels):
    """Run the test once per label, handed that guide block's text as ``example``."""

    def bind(test):
        for label in labels:
            BOUND.setdefault(label, []).append(test.__name__)
        return pytest.mark.parametrize("example", labels, indirect=True)(test)

    return bind


@pytest.fixture
def example(request):
    return examples()[request.param]


def test_every_guide_example_is_bound_and_every_binding_runs_it__D8():
    labels, bound = set(examples()), BOUND.keys() | APP_BOUND.keys()
    assert not labels - bound, f"labelled guide blocks no test reads: {sorted(labels - bound)}"
    assert not bound - labels, f"bindings to labels no guide has: {sorted(bound - labels)}"
    # pytest refuses a bound test without the ``example`` parameter; it must also use it.
    tests = {node.name: node for node in ast.parse(Path(__file__).read_text()).body
             if isinstance(node, ast.FunctionDef)}
    for name in {name for names in BOUND.values() for name in names}:
        used = {node.id for stmt in tests[name].body for node in ast.walk(stmt)
                if isinstance(node, ast.Name)}
        assert "example" in used, f"{name} is bound but never reads its example"
    for label, (path, name) in APP_BOUND.items():
        source = (WORKSPACE / path).read_text()
        (test,) = [node for node in ast.parse(source).body
                   if isinstance(node, ast.FunctionDef) and node.name == name]
        assert f"example={label}" in ast.get_source_segment(source, test), (path, name)
    # A whole definition, JSON record or renderer module cannot go unlabelled, and so unrun.
    for guide in GUIDES:
        for language, info, body in blocks(guide):
            value = yaml.safe_load(body) if language == "yaml" else None
            whole = (
                (isinstance(value, dict) and ("id" in value or "options" in value))
                or language == "json"
                or (language in MODULE_LANGUAGES and "import " in body
                    and "export default defineRenderer(" in body)
            )
            assert not whole or LABEL.search(info), f"{guide}: unlabelled example\n{body[:300]}"


def declaration(text):
    start = text.index("interface RendererAPI {")
    tokens = re.findall(
        r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_$][\w$]*|\d+|=>|\S',
        text[start:],
    )
    tokens = [t for t in tokens if not t.startswith(("//", "/*"))]
    depth, members, current = 0, {}, []
    for token in tokens[3:]:  # interface, name, {
        if token == "}" and depth == 0:
            break
        if token in ("(", "{", "[", "<"):
            depth += 1
        elif token in (")", "}", "]", ">"):
            depth -= 1
        if token == ";" and depth == 0:
            name = current[0]
            # Parameter spelling is not part of assignability; nested object
            # property spelling, optionality and every type token remain exact.
            normal, parens, braces = [], 0, 0
            for i, value in enumerate(current):
                if value == "(":
                    parens += 1
                elif value == ")":
                    parens -= 1
                elif value == "{":
                    braces += 1
                elif value == "}":
                    braces -= 1
                next_tokens = current[i + 1 : i + 3]
                if (
                    parens
                    and not braces
                    and re.fullmatch(r"[A-Za-z_$][\w$]*", value)
                    and (next_tokens[:1] == [":"] or next_tokens == ["?", ":"])
                ):
                    value = "$param"
                normal.append(value)
            members[name] = normal
            current = []
        else:
            current.append(token)
    assert members and not current
    return members


@binds("renderer-api-interface")
def test_guide_renderer_api_signature_and_optionality_match_canonical__D12(example):
    canonical = (ROOT / "packages/renderer-hooks/src/types.ts").read_text()
    assert declaration(example) == declaration(canonical)


@pytest.fixture(scope="module")
def compiled_handlers():
    from collimate._host.renderer_bundle import compile_verifier

    prelude = (ROOT / "shared/verify/prelude.js").read_bytes()
    return {
        name: compile_verifier(
            (BUILTINS / f"definitions/{name}/derived/verify.ts").read_bytes(), prelude=prelude
        ).code
        for name in ("renderer", "handler", "options")
    }


def verify(compiled_handlers, kind, text, *, path, package, paths):
    from collimate._host.verification import run_verifier

    return run_verifier(
        compiled_handlers[kind],
        {"path": path, "text": text, "package": package, "cell_paths": paths},
        deadline=time.monotonic() + 5,
    )


@binds("handler-example")
def test_actual_handler_guide_example_schema_and_covered_file__D12(compiled_handlers, example):
    from collimate.handlers import validate_handler

    raw = example
    value = validate_handler(yaml.safe_load(raw))
    folder = value["examples"][0]["folder"]
    guide = (DOCS / GUIDES[1]).read_text()
    sample = re.search(r"Put the sample at `([^`]+)`", guide).group(1)
    assert sample.startswith("examples/" + folder + "/")
    assert sample.endswith(value["types"][0]["ext"])
    package = {sample: "# A heading\n\nAn explanatory paragraph."}
    args = dict(path="note/note.handler", package=package, paths=["note/" + sample])
    assert verify(compiled_handlers, "handler", raw, **args) == {"valid": True}
    assert verify(compiled_handlers, "handler", raw, **{**args, "package": {}})["valid"] is False


@binds("options-example")
def test_actual_options_guide_example_maps_and_validates__D12(compiled_handlers, example):
    from collimate.options_stack import parse_options_stack, map_options_stack

    raw = example
    path = "choices.options_stack"
    paths = [path, "first/a.txt", "second/b.txt", "brief.md", "kept.txt"]
    document = parse_options_stack(path, raw)
    plan = map_options_stack(document, paths)
    assert plan.options == [
        {"name": "First", "files": {"a.txt": "first/a.txt", "brief.md": "brief.md"}},
        {"name": "Second", "files": {"b.txt": "second/b.txt", "brief.md": "brief.md"}},
    ]
    assert plan.kept == {"kept.txt"} and plan.retain_source
    assert verify(compiled_handlers, "options", raw, path=path, package={}, paths=paths) == {
        "valid": True
    }
    assert (
        verify(compiled_handlers, "options", raw, path=path, package={}, paths=[path, "brief.md"])[
            "valid"
        ]
        is False
    )


@binds(
    "renderer-full-schema",
    "renderer-manifest-text",
    "renderer-manifest-html",
    "renderer-manifest-website",
    "renderer-manifest-markdown",
    "renderer-example-1-manifest",
    "renderer-example-2-manifest",
)
def test_actual_renderer_guide_manifests_pass_native_handler__D12(compiled_handlers, example):
    source = 'import {defineRenderer} from "/api/sdk/react.js"; export default defineRenderer({component:()=>null});'
    package = {yaml.safe_load(example)["entrypoint"]: source}
    assert verify(
        compiled_handlers, "renderer", example, path="demo/demo.renderer", package=package, paths=[]
    ) == {"valid": True}


@binds(
    "renderer-style-control",
    "renderer-imperative-dom",
    "renderer-example-1-module",
    "renderer-example-2-module",
    "renderer-example-3-module",
    "renderer-example-4-module",
    "renderer-example-5-module",
    "renderer-tokens-usage",
)
def test_complete_documented_renderer_modules_compile_and_pass_shape__D12(
    compiled_handlers, example
):
    raw = yaml.safe_dump(
        {
            "id": "example",
            "name": "Example",
            "summary": "Documented example",
            "entrypoint": "main.tsx",
        }
    )
    assert verify(
        compiled_handlers,
        "renderer",
        raw,
        path="example/example.renderer",
        package={"main.tsx": example},
        paths=[],
    ) == {"valid": True}


def test_renderer_guide_monaco_example_pins_the_shipped_version_X01():
    """Example 2 loads the Monaco build the shipped renderer loads."""
    pin = re.compile(r"https://cdn\.jsdelivr\.net/npm/monaco-editor@([^/]+)/min/vs'")
    shipped = pin.findall((RENDERERS / "documents/monaco/main.tsx").read_text())
    documented = [
        found for language, _, body in blocks(GUIDES[0]) if language == "tsx"
        for found in pin.findall(body)
    ]
    assert len(shipped) == 1 and documented == shipped


@pytest.fixture(scope="module")
def session_trace_script(tmp_path_factory):
    """A node script replaying retry traces against the real session helper."""
    import subprocess

    tmp_path = tmp_path_factory.mktemp("session")
    module = tmp_path / "session.mjs"
    build = subprocess.run(
        [
            str(ROOT / "scripts/node_modules/esbuild/bin/esbuild"),
            str(ROOT / "packages/renderer-hooks/src/session.ts"),
            "--format=esm",
            f"--outfile={module}",
        ],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    script = tmp_path / "trace.mjs"
    script.write_text("""
import assert from 'node:assert/strict';
const {openSessionSocket}=await import(process.argv[2]);
const traces=JSON.parse(process.argv[3]);
for(const trace of traces){
 const sockets=[],timers=[];
 globalThis.setTimeout=fn=>{timers.push(fn);return timers.length};globalThis.clearTimeout=()=>{};
 globalThis.WebSocket=class{static OPEN=1;constructor(){sockets.push(this)}close(){}};
 const connection=openSessionSocket({url:'ws://localhost/s',maxFailedOpens:trace.maxFailedOpens,transientRetryMs:0});
 for(let i=0;i<trace.attempts.length;i++){
  if(i)timers.shift()();
  for(const event of trace.attempts[i]){
   if(event==='open')sockets[i].onopen();
   else if(event==='message')sockets[i].onmessage({data:'frame'});
   else sockets[i].onclose({code:Number(event.split(':')[1])});
  }
 }
 assert.equal(connection.status,trace.status);connection.close();
}
""")
    return script, module


@binds("renderer-retry-ended", "renderer-retry-reset")
def test_documented_retry_traces_execute_real_session_helper__D12(session_trace_script, example):
    import subprocess

    script, module = session_trace_script
    result = subprocess.run(
        ["node", str(script), module.as_uri(), json.dumps([json.loads(example)])],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@binds("renderer-tokens-table")
def test_documented_theme_token_table_matches_actual_export__D12_R01(example):
    import subprocess

    source = (ROOT / "packages/renderer-hooks/src/index.tsx").read_text()
    expression = source.split("export const tokens = ", 1)[1].split("\n});", 1)[0] + "\n})"
    result = subprocess.run(
        ["node", "-e", "process.stdout.write(JSON.stringify(" + expression + "));"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    groups = {k for k, v in actual.items() if isinstance(v, dict)}
    scalars = {k: v for k, v in actual.items() if isinstance(v, str)}
    assert groups == {"color", "font"} and set(scalars) == {"radius", "shadow", "lift"}
    guide = (DOCS / GUIDES[0]).read_text()
    # The tokens code table lists every group's keys and every scalar's value.
    table = re.sub(r"//[^\n]*", "", example)
    documented = dict(re.findall(r"tokens\.(\w+)\s*=\s*\{(.*?)\}", table, re.S))
    assert set(documented) == groups
    for group, body in documented.items():
        assert set(re.findall(r"\b\w+\b", body)) == set(actual[group])
        assert all(value.startswith("var(--") for value in actual[group].values())
    assert dict(re.findall(r"tokens\.(\w+)\s*=\s*'([^']*)'", table)) == scalars
    # The theming table maps every CSS name to its tokens member, exactly.
    names = re.findall(r"'(--[a-z0-9-]+)'", source.split("export const THEME_VAR_NAMES", 1)[1].split("] as const", 1)[0])
    rows = re.findall(r"^\| `(--[a-z0-9-]+)` \| `([\w.]+)` \| [^|]+\|$", guide, re.M)
    assert [name for name, _ in rows] and sorted(name for name, _ in rows) == sorted(names)
    for name, member in rows:
        value = actual
        for part in member.split("."):
            value = value[part]
        assert value == f"var({name})", (name, member)
    references = re.findall(r"tokens\.(\w+)(?:\.(\w+))?", guide)
    assert references
    for group, key in references:
        assert group in actual, group
        if key:
            assert isinstance(actual[group], dict) and key in actual[group], (group, key)


@binds("renderer-theme-interface")
def test_documented_renderer_theme_fields_match_canonical_R01(example):
    def fields(text):
        body = text.split("interface RendererTheme {", 1)[1].split("\n}", 1)[0]
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        body = re.sub(r"//[^\n]*", "", body)
        return re.findall(r"^\s*(\w+)\??:", body, re.M)

    documented = example
    canonical = (ROOT / "packages/renderer-hooks/src/types.ts").read_text()
    assert fields(documented) == fields(canonical) == ["themeId", "scheme", "known", "vars", "fonts"]
    assert "Record<ThemeVarName, string>" in documented and "ui: string; mono: string; prose: string" in documented


@binds("options-literal-walkthrough")
def test_options_guide_literal_walkthrough_uses_real_mapper__D12(compiled_handlers, example):
    from collimate.options_stack import parse_options_stack, map_options_stack

    rows = re.findall(r"^\| `([^`]+)` \| `([^`]+)` \| `([^`]+)` \| `([^`]+)` \|$", example, re.M)
    assert len(rows) == 6
    for root, pattern, available, outputs in rows:
        path = "choices.options_stack"
        paths = [path, *available.split(", ")]
        raw = yaml.safe_dump(
            {"options": [{"name": "Example", "root": root, "files": [pattern]}], "rest": "drop"}
        )
        plan = map_options_stack(parse_options_stack(path, raw), paths)
        assert set(plan.options[0]["files"]) == set(outputs.split(", "))
        assert verify(compiled_handlers, "options", raw, path=path, package={}, paths=paths) == {
            "valid": True
        }


@binds("renderer-full-schema")
def test_documented_renderer_required_fields_match_real_schema__D12(compiled_handlers, example):
    raw = example
    required_block = re.split(
        r"^# [A-Z][A-Z ]+", raw.split("# REQUIRED FIELDS", 1)[1], maxsplit=1, flags=re.M
    )[0]
    documented_required = set(yaml.safe_load(required_block))
    schema = json.loads((ROOT / "shared/verify/renderer-schemas.json").read_text())["renderer"]
    assert documented_required == set(schema["required"])
    value = yaml.safe_load(raw)
    for key in value:
        changed = {name: item for name, item in value.items() if name != key}
        verdict = verify(
            compiled_handlers,
            "renderer",
            yaml.safe_dump(changed),
            path="doc/doc.renderer",
            package={
                value[
                    "entrypoint"
                ]: 'import {defineRenderer} from "/api/sdk/react.js"; export default defineRenderer({component:()=>null});'
            },
            paths=[],
        )
        assert verdict["valid"] is (key not in documented_required), (key, verdict)
