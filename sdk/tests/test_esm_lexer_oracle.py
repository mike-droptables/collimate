# SPDX-License-Identifier: MIT
"""The Python import scan against es-module-lexer 3.0.4's minimal build itself.

``scripts/esm_lexer_oracle.mjs`` runs the real lexer (a devDependency of
``scripts/package.json``) over the same inputs: every JavaScript body of the
committed module snapshot, and every source es-module-lexer's own unit suite
parses (``tests/fixtures/esm_lexer_unit_sources.json``). Each must give the
same imports (specifier, span, dynamic or not) or the same parse error.
The snapshot pass also holds the mirror's rewrite to its contract: no served
body keeps a root-relative specifier or an absolute one for its own host.

Needs Node and the SDK's ``scripts/node_modules`` (``npm ci --prefix scripts``).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from collimate._host.esm_lexer import LexError, scan_imports
from collimate._host.module_mirror import is_javascript, read_snapshot

from tests._workspace import MODULE_SNAPSHOT as SNAPSHOT

ROOT = Path(__file__).resolve().parents[1]
ORACLE = ROOT / "scripts/esm_lexer_oracle.mjs"
UNIT_SOURCES = ROOT / "tests/fixtures/esm_lexer_unit_sources.json"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (ROOT / "scripts/node_modules/es-module-lexer").is_dir(),
    reason="needs node and scripts/node_modules (npm ci --prefix scripts)")


def _oracle(inputs: dict, tmp_path: Path) -> list[dict]:
    request = tmp_path / "oracle-inputs.json"
    request.write_text(json.dumps(inputs))
    run = subprocess.run(["node", str(ORACLE), str(request)], capture_output=True, text=True,
                         cwd=str(ROOT), timeout=600)
    assert run.returncode == 0, run.stderr
    return [json.loads(line) for line in run.stdout.splitlines()]


def _ours(source: str) -> dict:
    try:
        records = scan_imports(source)
    except LexError as error:
        return {"error": error.position}
    return {"imports": [[r.name, r.start, r.end, r.dynamic] for r in records]}


def _expected(result: dict) -> dict:
    """The oracle's record in the port's shape: import.meta dropped, a literal
    dynamic import's span narrowed to the text inside its quotes."""
    if "error" in result:
        return result
    out = []
    for name, start, end, dynamic in result["imports"]:
        if dynamic == -2:
            continue
        if dynamic == -1:
            out.append([name, start, end, False])
        elif name is not None:
            out.append([name, start + 1, end - 1, True])
        else:
            out.append([None, start, end, True])
    return {"imports": out}


def _same(ours: dict, oracle: dict, length: int) -> bool:
    if "error" in ours or "error" in oracle:
        # Past the end of the input the C lexer reads stale memory, so only an
        # error inside the source has a reproducible offset.
        return ("error" in ours and "error" in oracle
                and (oracle["error"] >= length or ours["error"] == oracle["error"]))
    return ours == oracle


def _mismatches(labels, sources_or_results, oracle, lengths) -> list[str]:
    bad = []
    for label, ours, expected, length in zip(labels, sources_or_results, oracle, lengths):
        expected = _expected(expected)
        if not _same(ours, expected, length):
            bad.append(f"{label}\n    port:   {json.dumps(ours)[:400]}\n    oracle: {json.dumps(expected)[:400]}")
    return bad


def test_unit_suite_sources_match_es_module_lexer(tmp_path):
    sources = json.loads(UNIT_SOURCES.read_text(encoding="utf-8"))["sources"]
    # The suite's one generated case: a 5 MiB string between an import and an export.
    sources.append("import 'a';\nconst x = \"" + "a" * (5 * 1024 * 1024) + "\";\nexport { x }")
    assert len(sources) > 150
    oracle = _oracle({"sources": sources}, tmp_path)
    assert len(oracle) == len(sources)
    bad = _mismatches([repr(s[:120]) for s in sources], [_ours(s) for s in sources], oracle,
                      [len(s) for s in sources])
    assert not bad, f"{len(bad)} unit source(s) lex differently:\n" + "\n".join(bad)


@pytest.fixture(scope="module")
def snapshot_corpus(tmp_path_factory):
    """Every served JavaScript body of the snapshot, with the oracle's reading."""
    snapshot = read_snapshot(SNAPSHOT)
    modules = sorted((url, SNAPSHOT / "modules" / entry.hash)
                     for url, entry in snapshot.entries.items() if is_javascript(entry.media_type))
    oracle = _oracle({"files": [str(path) for _, path in modules]}, tmp_path_factory.mktemp("oracle"))
    assert len(oracle) == len(modules)
    return modules, oracle


def test_snapshot_corpus_matches_es_module_lexer(snapshot_corpus):
    modules, oracle = snapshot_corpus
    assert len(modules) > 1000, "the snapshot corpus is unexpectedly small"
    sources = [path.read_text(encoding="utf-8") for _, path in modules]
    bad = _mismatches([url for url, _ in modules], [_ours(source) for source in sources], oracle,
                      [len(source) for source in sources])
    assert not bad, f"{len(bad)} snapshot module(s) lex differently:\n" + "\n".join(bad[:20])
    assert not any("error" in result for result in oracle), "a served module does not lex"


def test_no_served_body_keeps_a_root_relative_or_own_host_specifier(snapshot_corpus):
    modules, oracle = snapshot_corpus
    bad = []
    for (url, _), result in zip(modules, oracle):
        own_host = url[:url.index("/", len("https://")) + 1]
        for name, *_ in result.get("imports", []):
            if name is None:
                continue
            absolute = "https:" + name if name.startswith("//") else name
            if (name.startswith("/") and not name.startswith("//")) or absolute.startswith(own_host):
                bad.append(f"{url}: {name}")
    assert not bad, "served bodies keep specifiers the mirror route cannot resolve:\n" + "\n".join(bad[:20])
