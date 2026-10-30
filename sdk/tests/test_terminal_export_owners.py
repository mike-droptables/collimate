"""Terminal readers share callable exporters and the canonical harness roster."""
import asyncio
import dataclasses
import json
import os
import stat
from pathlib import Path

import pytest

from tests._commands_matrix import recording_runtime  # noqa: F401,E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402


def test_public_exporters_read_the_same_native_session_F1493(tmp_path):
    from _agent_shared.harnesses.claude_code import backend as claude
    from _agent_shared.harnesses.codex import backend as codex
    from _agent_shared.harnesses.goose import backend as goose
    from _agent_shared.harnesses.opencode import backend as opencode
    from _agent_shared import terminal_tail as tail

    assert hasattr(claude, 'claude_session_paths')
    assert hasattr(claude, 'pull_claude_transcript')
    assert hasattr(codex, 'newest_codex_session_path')
    assert hasattr(goose, 'goose_session_export')
    assert hasattr(opencode, 'text_from_parts')
    assert hasattr(opencode, 'cli_output')
    project = '/workspace/punctuation.and spaces'
    path = Path(claude.claude_session_paths(str(tmp_path), project, 'current')[0])
    path.parent.mkdir(parents=True)
    body = 'Complete answer λ\n' * 300
    raw = json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': body}}) + '\n'
    path.write_text(raw)
    rows, actual = claude.pull_claude_transcript('current', store_dir=str(tmp_path), project=project)
    assert actual == raw and rows == [{'role': 'assistant', 'content': body}]
    assert tail._ClaudeTail('current', str(tmp_path), project).read()[0].content == body
    assert opencode.text_from_parts([{'type': 'text', 'text': body}]) == body
    assert tail.messages_from_opencode_export({'messages': [
        {'info': {'role': 'assistant'}, 'parts': [{'type': 'text', 'text': body}]}]})[0].content == body.strip()
    for module, retired in ((claude, ('_claude_session_paths', '_pull_claude_transcript')),
                            (codex, ('_newest_codex_session_path',)),
                            (goose, ('_goose_session_export',)),
                            (opencode, ('_text_from_parts', '_cli_output'))):
        assert all(not hasattr(module, name) for name in retired)


def test_tail_availability_and_factory_follow_one_roster_F1493(monkeypatch):
    from _agent_shared import terminal_tail as tail
    row = tail.HARNESS['opencode']
    observed = []
    sentinel = object()
    monkeypatch.setattr(tail, 'review_reader', lambda sid, before: observed.append((sid, before)) or sentinel,
                        raising=False)
    monkeypatch.setitem(tail.HARNESS, 'opencode', dataclasses.replace(
        row, terminal=dataclasses.replace(row.terminal, tail='terminal_tail:review_reader')))
    assert tail.has_tail_reader('opencode')
    assert tail.make_tail_reader('opencode', harness_session_id='current', before=frozenset({'old'})) is sentinel
    assert observed == [('current', frozenset({'old'}))]
    monkeypatch.setitem(tail.HARNESS, 'opencode', dataclasses.replace(
        row, terminal=dataclasses.replace(row.terminal, tail=None)))
    assert not tail.has_tail_reader('opencode')
    assert tail.make_tail_reader('opencode', harness_session_id='current') is None


@pytest.mark.parametrize('leg,runtime', [('acp', 'docker'), ('acp', 'standard'), ('terminal', 'docker')])
def test_gemini_selected_native_auth_is_identical_across_legs_F1407(recording_runtime, leg, runtime):  # noqa: F811
    from _agent_shared import harness_homes
    prepared = asyncio.run(_pipeline.prepare(_HARNESS['opencode'], {
        'auth': 'api_key', 'provider': 'gemini', 'model': 'gemini-test-model',
        'commands': 'deny', 'websearch': False, 'system_prompt': '',
        'runtime': runtime, 'host_harness': {'path': '/fake/opencode', 'version': '1.18.30'},
    }, leg=leg))
    root = Path(harness_homes.harness_home('opencode'))
    auth = root / ('data/opencode/auth.json' if runtime == 'standard' else 'auth.json')
    assert json.loads(auth.read_text()) == {'google': {'type': 'api', 'key': 'matrix-fake-key'}}
    assert stat.S_IMODE(os.stat(auth).st_mode) == 0o600
    assert prepared.model == 'google/gemini-test-model'
    assert 'GEMINI_API_KEY' not in prepared.env and 'GOOGLE_GENERATIVE_AI_API_KEY' not in prepared.env
