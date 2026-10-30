"""Private normalized diagnostics do not replace the native ACP witness."""
import ast
import json
import os
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'tests_live/test_live_codex_qwen_acp_capture.py'


def writer():
    tree = ast.parse(SOURCE.read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name == 'write_reply_diagnostic']
    assert len(nodes) == 1, 'Actual fixture lacks retained normalized reply diagnostic'
    scope = {'json': json, 'os': os, 'MAX_REPLY_DIAGNOSTIC_BYTES': 256 * 1024}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), scope)
    return scope['write_reply_diagnostic']


def test_actual_fixture_retains_scrubbed_reply_with_private_mode(tmp_path):
    path = tmp_path / 'reply.json'
    writer()(path, engine_returned=True, text='answer secret-key secret-key',
             exception_type=None, selected_key='secret-key')
    value = json.loads(path.read_text())
    assert value['text'] == 'answer [REDACTED] [REDACTED]'
    assert value['engine_returned'] is True and value['exception_type'] is None
    assert value['authority'] == 'normalized_diagnostic_only'
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('returned,text,error', [(False, None, 'TimeoutError'),
                                                (False, None, 'CancelledError'),
                                                (True, '', 'AssertionError')])
def test_return_and_exception_diagnostic_are_distinct(tmp_path, returned, text, error):
    path = tmp_path / 'reply.json'
    writer()(path, engine_returned=returned, text=text, exception_type=error, selected_key='secret')
    value = json.loads(path.read_text())
    assert (value['engine_returned'], value['text'], value['exception_type']) == (returned, text, error)


def test_existing_artifact_cannot_be_replaced(tmp_path):
    path = tmp_path / 'reply.json'
    path.write_text('previous')
    with pytest.raises(FileExistsError):
        writer()(path, engine_returned=False, text=None, exception_type='Error', selected_key='secret')
    assert path.read_text() == 'previous'


def test_symlink_cannot_touch_target(tmp_path):
    target, path = tmp_path / 'target', tmp_path / 'reply.json'
    target.write_text('private original')
    path.symlink_to(target)
    with pytest.raises(OSError):
        writer()(path, engine_returned=False, text=None, exception_type=None, selected_key='secret')
    assert target.read_text() == 'private original'


def test_oversized_diagnostic_fails_before_creation(tmp_path):
    path = tmp_path / 'reply.json'
    with pytest.raises(AssertionError, match='bound'):
        writer()(path, engine_returned=True, text='x' * (256 * 1024),
                 exception_type=None, selected_key='secret')
    assert not path.exists()


def test_actual_fixture_writes_diagnostic_in_finally_after_engine_return():
    tree = ast.parse(SOURCE.read_text())
    fixture = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == 'test_capture_actual_codex_qwen_native_generator_allow_D23')
    final = next(node for node in fixture.body if isinstance(node, ast.Try))
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == 'write_reply_diagnostic' for node in ast.walk(ast.Module(
                   body=final.finalbody, type_ignores=[])))
    assignments = [node for node in ast.walk(final) if isinstance(node, ast.Assign)]
    assert any(isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
               and node.value.func.id == 'run_on_test_loop'
               and any(isinstance(target, ast.Name) and target.id == 'result' for target in node.targets)
               for node in assignments)


@pytest.mark.parametrize('failure', [TimeoutError, KeyboardInterrupt])
def test_actual_fixture_exception_finally_writes_reply_and_preserves_error(tmp_path, monkeypatch, failure):
    tree = ast.parse(SOURCE.read_text())
    fixture = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == 'test_capture_actual_codex_qwen_native_generator_allow_D23')
    actual_try = next(node for node in fixture.body if isinstance(node, ast.Try))
    raw_paths = []
    class Capture:
        def write_private(self, path):
            raw_paths.append(path)
    def engine_return(turn):
        assert turn == 'unchanged-turn'
        raise failure('private engine error secret')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'secret')
    scope = {'engine_returned': False, 'reply_text': None, 'exception_type': None,
             'run_on_test_loop': engine_return, 'turn': lambda: 'unchanged-turn',
             'capture': Capture(), 'tmp_path': tmp_path, 'backend': 'qwen-code',
             'write_reply_diagnostic': writer(), 'os': os}
    with pytest.raises(failure, match='private engine error'):
        exec(compile(ast.Module(body=[actual_try], type_ignores=[]), str(SOURCE), 'exec'), scope)
    value = json.loads((tmp_path / 'qwen-code-normalized-reply.json').read_text())
    assert value['engine_returned'] is False and value['text'] is None
    assert value['exception_type'] == failure.__name__
    assert 'private engine error' not in json.dumps(value)
    assert raw_paths == [tmp_path / 'qwen-code-raw-acp.json']
