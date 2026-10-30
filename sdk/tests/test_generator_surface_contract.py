"""Shipped generators declare their generic SDK surface and private-file rules."""
import ast

import pytest

from tests._workspace import GENERATORS


ROOT = GENERATORS


def test_agent_mirrors_declare_private_paths__F1233(monkeypatch):
    calls = []
    for folder in (ROOT / '_agent_shared', ROOT / 'agent'):
        for path in folder.rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'live_files':
                    calls.append(path)
                    skip = next((k.value for k in node.keywords if k.arg == 'skip_globs'), None)
                    assert isinstance(skip, ast.Name) and skip.id == 'AGENT_LIVE_SKIP_GLOBS', str(path)
    assert sorted(calls) == sorted([ROOT / '_agent_shared/terminal_runner.py',
                                    ROOT / '_agent_shared/session/entry.py'])
    monkeypatch.syspath_prepend(str(ROOT))
    from _agent_shared import surface_runner
    from _agent_shared.harnesses import HARNESS, Terminal
    from _agent_shared.run_files import AGENT_LIVE_SKIP_GLOBS
    from _agent_shared.terminal_runner import run_terminal
    assert AGENT_LIVE_SKIP_GLOBS == ('_agent', '_agent/**', '.pydantic-ai-harness', '.pydantic-ai-harness/**')
    for name, row in HARNESS.items():
        if row.terminal is not None:
            assert isinstance(row.terminal, Terminal)
            assert surface_runner(name, 'terminal') is run_terminal
        else:
            with pytest.raises(ValueError, match="has no 'terminal' surface"):
                surface_runner(name, 'terminal')


def test_model_writer_passes_its_full_path__F1169(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    from _agent_shared import run_files
    writes = []
    path = '_runs/test-1234abcd/chat/notes.md'
    monkeypatch.setattr(run_files, 'write_refusal', lambda p: None)
    monkeypatch.setattr(run_files.gen, 'update_file', lambda p, text: writes.append((p, text)))
    run_files.write_file(path, 'notes')
    assert writes == [(path, 'notes')]


