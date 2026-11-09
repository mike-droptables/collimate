# SPDX-License-Identifier: MIT
"""Credential-free execution of the composed pinned native Cline live oracles."""

import ast
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading

import pytest

from tests._workspace import GENERATORS

SOURCE = Path(__file__).resolve().parents[1] / "tests_live/test_live_cline_commands.py"
COMMAND = "exact shell command"
DIGEST = "expected-digest"
PROMPT = "exact synthetic user request"
MODEL = "requested"


@pytest.fixture(autouse=True)
def canonical_agent_dependencies(monkeypatch):
    """Load real writer dependencies without another test module's path setup."""
    monkeypatch.syspath_prepend(str(GENERATORS))


def actual():
    names = {
        "command_input",
        "native_tool_records",
        "pending_command_call",
        "terminal_checkpoint",
        "capture_terminal_evidence",
        "fresh_approval_panel",
        "positioned_approval_rows",
        "answer_native_approval",
        "terminal_command_candidate",
        "unavailable_command_refusal",
        "terminal_command_outcome",
        "native_sessions",
        "native_conversation",
        "owned_native_path",
        "native_message_text",
        "command_child_running",
        "positioned_question_rows",
        "pending_rejection_clarification",
        "fresh_question_panel",
        "answer_native_rejection_clarification",
    }
    tree = ast.parse(SOURCE.read_text())
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {node.name for node in functions} == names
    namespace = {
        "json": json,
        "sqlite3": sqlite3,
        "Path": Path,
        "os": os,
        "stat": stat,
        "re": re,
        "datetime": datetime,
        "cline": type("NativeConstants", (), {"CLINE_HOME": "/home/coder/.cline"}),
    }
    constants = [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id in
                         {'CLARIFICATION_RESPONSE', 'USER_REJECTION'} for target in node.targets)]
    exec(compile(ast.Module(body=constants + functions, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


@pytest.fixture
def local_command_environment(monkeypatch, tmp_path):
    """Actual SDK argv wrapper, real processes, local transport instead of Docker."""
    import subprocess
    import gen
    from _env_shared import DockerEnvironment

    environment = DockerEnvironment.__new__(DockerEnvironment)
    environment._env_id = 'local-scanner-control'
    environment.mount = str(tmp_path)
    calls = []

    def local_exec(env_id, argv, **kwargs):
        assert env_id == environment._env_id
        assert kwargs['user'] == 'root' and kwargs['timeout'] == 5
        assert argv[:4] == ['timeout', '5', 'python3', '-c']
        calls.append(list(argv))
        result = subprocess.run(argv, cwd=kwargs['cwd'], capture_output=True, text=True, timeout=10)
        return result.returncode, result.stdout

    monkeypatch.setattr(gen, 'exec_environment', local_exec)
    return environment, calls


def test_actual_command_scanner_excludes_only_proven_own_timeout_parent_F1560(local_command_environment):
    import uuid

    environment, calls = local_command_environment
    command = 'sleep 30; : scanner-' + uuid.uuid4().hex
    # No target is launched. The actual wrapper's timeout alone carries the
    # target bytes; the old scanner returns True here.
    assert actual()['command_child_running'](environment, command) is False
    assert len(calls) == 1 and calls[0][-1] == command


@pytest.mark.parametrize('kind', ['shell', 'direct', 'unrelated-timeout'])
def test_actual_command_scanner_retains_real_command_bearing_processes_F1560(
    local_command_environment, kind
):
    import signal
    import subprocess
    import sys
    import time
    import uuid

    environment, _ = local_command_environment
    command = 'printf "ready\\n"; sleep 30; : scanner-' + uuid.uuid4().hex
    if kind == 'shell':
        argv = ['sh', '-c', command]
    elif kind == 'direct':
        argv = [sys.executable, '-c', 'import time;print("ready",flush=True);time.sleep(30)', command]
    else:
        # The exec removes the target from the worker's argv. Only this
        # unrelated timeout still carries it; skipping every timeout fails.
        argv = ['timeout', '30', sys.executable, '-c',
                'import os;print("ready",flush=True);os.execl("/bin/sleep","sleep","30")', command]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert process.stdout.readline().strip() == 'ready'
        if kind == 'unrelated-timeout':
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                children = Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()
                if len(children) == 1:
                    child_args = Path(f'/proc/{children[0]}/cmdline').read_bytes()
                    if child_args == b'sleep\x0030\x00':
                        break
                time.sleep(0.01)
            else:
                pytest.fail('Unrelated timeout worker did not exec sleep within two seconds')
            assert command.encode() in Path(f'/proc/{process.pid}/cmdline').read_bytes()
        assert actual()['command_child_running'](environment, command) is True
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
        process.stdout.close()


@pytest.mark.parametrize('damage', ['duration', 'argv0', 'no_timeout',
                                   'status_unreadable', 'exe_unreadable'])
def test_actual_command_scanner_refuses_unproven_parent_ancestry_F1560(
    local_command_environment, monkeypatch, damage
):
    import gen
    import subprocess
    import uuid

    environment, _ = local_command_environment
    results = []

    def wrong_ancestry(env_id, argv, **kwargs):
        changed = list(argv)
        if damage == 'duration': changed[1] = '6'
        elif damage == 'argv0': changed[0] = '/usr/bin/timeout'
        elif damage == 'no_timeout': changed = changed[2:]
        elif damage == 'status_unreadable':
            changed[4] = ('import pathlib\n'
                          'pathlib.Path.read_text=lambda *a,**k: (_ for _ in ()).throw('
                          'PermissionError("Unreadable scanner ancestry"))\n' + changed[4])
        else:
            changed[4] = ('import os\n'
                          'os.readlink=lambda *a,**k: (_ for _ in ()).throw('
                          'PermissionError("Unreadable scanner executable"))\n' + changed[4])
        result = subprocess.run(changed, capture_output=True, text=True, timeout=10)
        results.append(result)
        return result.returncode, result.stdout

    monkeypatch.setattr(gen, 'exec_environment', wrong_ancestry)
    with pytest.raises(AssertionError):
        actual()['command_child_running'](environment, 'unlaunched-' + uuid.uuid4().hex)
    assert len(results) == 1 and results[0].returncode != 0
    assert results[0].stdout.strip() not in ('True', 'False')


def messages(level="allow", *, pending=False):
    rows = [{"id": "user-1", "role": "user", "content": [{"type": "text", "text": PROMPT}]}]
    if level != "deny":
        rows.append(
            {
                "id": "assistant-call",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "run_commands",
                        "input": {"commands": [COMMAND]},
                    }
                ],
            }
        )
        if pending:
            return rows
        rows.append(
            {
                "id": "result-1",
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "name": "run_commands",
                        "content": "User rejected" if level == "ask-reject" else DIGEST,
                        "is_error": level == "ask-reject",
                    }
                ],
            }
        )
    rows.append(
        {
            "id": "assistant-final",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "NO-SHELL" if level == "deny" else "native final response"}
            ],
        }
    )
    return rows


def row(status="idle"):
    return {
        "session_id": "own",
        "provider": "openrouter",
        "model": MODEL,
        "messages_path": "/home/coder/.cline/sessions/own/own.messages.json",
        "status": status,
        "ended_at": None if status == "idle" else "2026-10-04T00:00:01Z",
        "exit_code": None if status == "idle" else 0,
        "started_at": "2026-10-04T00:00:00Z",
        "prompt": '<user_input mode="act">' + PROMPT + "</user_input>",
        "cwd": "/workspace",
        "workspace_root": "/workspace",
        "interactive": 1,
        "is_subagent": 0,
    }


def manifest(current):
    return {
        key: current[key]
        for key in ("session_id", "provider", "model", "status", "ended_at", "exit_code")
    }


def snapshot(level="allow", status="idle"):
    current = row(status)
    return {"row": current, "messages": messages(level), "manifest": manifest(current)}


def respond(level):
    return {"call-1": level} if level.startswith("ask") else {}


def write_native(home, level="allow", status="idle"):
    home.mkdir(exist_ok=True)
    current = row(status)
    path = home / "sessions/own/own.messages.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "sessionId": "own", "messages": messages(level)}))
    (path.parent / "own.json").write_text(json.dumps(manifest(current)))
    columns = ",".join(
        f"{key} " + ("INTEGER" if key in ("interactive", "is_subagent", "exit_code") else "TEXT")
        for key in current
    )
    with sqlite3.connect(home / "sessions.db") as db:
        db.execute("CREATE TABLE sessions(" + columns + ")")
        db.execute(
            "INSERT INTO sessions VALUES(" + ",".join("?" for _ in current) + ")",
            tuple(current.values()),
        )
    return path


def read_native(home):
    return actual()["native_conversation"](home, "own", MODEL, PROMPT)


@pytest.mark.parametrize("level", ["deny", "ask-reject", "ask-allow", "allow"])
def test_actual_cline_local_sqlite_composed_completion_without_hub_events_F1560(tmp_path, level):
    home = tmp_path / "home"
    write_native(home, level)
    before = read_native(home)
    marker = "" if level in ("deny", "ask-reject") else DIGEST
    helper = actual()
    assert helper["terminal_command_candidate"](
        before, COMMAND, DIGEST, level, marker, respond(level)
    )
    assert not helper["terminal_command_outcome"](
        before, before, COMMAND, DIGEST, level, marker, respond(level)
    )
    with sqlite3.connect(home / "sessions.db") as db:
        db.execute(
            "UPDATE sessions SET status='completed',ended_at='2026-10-04T00:00:01Z',exit_code=0"
        )
    (home / "sessions/own/own.json").write_text(json.dumps(manifest(row("completed"))))
    assert helper["terminal_command_outcome"](
        before, read_native(home), COMMAND, DIGEST, level, marker, respond(level)
    )


@pytest.mark.parametrize(
    "damage",
    [
        "idle",
        "running",
        "pending",
        "failed",
        "cancelled",
        "missing_end",
        "exit",
        "bool_exit",
        "end_before_start",
        "invalid_end",
        "session",
        "model",
        "prompt",
        "changed_messages",
        "manifest_status",
        "manifest_session",
        "manifest_exit",
        "manifest_end",
    ],
)
def test_actual_cline_persisted_completion_refuses_false_terminal_verdict_F1560(damage):
    before, after = snapshot(), snapshot(status="completed")
    if damage in ("idle", "running", "pending", "failed", "cancelled"):
        after["row"]["status"] = damage
    elif damage == "missing_end":
        after["row"]["ended_at"] = None
    elif damage == "exit":
        after["row"]["exit_code"] = 1
    elif damage == "bool_exit":
        after["row"]["exit_code"] = False
    elif damage == "end_before_start":
        after["row"]["ended_at"] = "2026-10-03T23:59:59Z"
    elif damage == "invalid_end":
        after["row"]["ended_at"] = "not-a-time"
    elif damage in ("session", "model", "prompt"):
        after["row"][{"session": "session_id", "model": "model", "prompt": "prompt"}[damage]] = (
            "foreign"
        )
    elif damage == "changed_messages":
        after["messages"][-1]["id"] = "different"
    else:
        after["manifest"][
            damage.removeprefix("manifest_")
            .replace("session", "session_id")
            .replace("exit", "exit_code")
            .replace("end", "ended_at")
        ] = "foreign"
    assert not actual()["terminal_command_outcome"](
        before, after, COMMAND, DIGEST, "allow", DIGEST, {}
    )


@pytest.mark.parametrize(
    "damage",
    [
        "wrong_command",
        "wrong_tool",
        "result_id",
        "result_name",
        "result_before_call",
        "duplicate_call",
        "duplicate_result",
        "missing_result",
        "error",
        "echo_only",
        "missing_marker",
        "no_final",
        "final_tool",
        "error_assistant",
        "nonidle",
        "unpaired_other_call",
    ],
)
def test_actual_cline_candidate_requires_complete_exact_native_history_F1560(damage):
    value = snapshot()
    marker = DIGEST
    call = value["messages"][1]["content"][0]
    result = value["messages"][2]["content"][0]
    if damage == "wrong_command":
        call["input"]["commands"] = ["foreign"]
    elif damage == "wrong_tool":
        call["name"] = "read_files"
    elif damage == "result_id":
        result["tool_use_id"] = "foreign"
    elif damage == "result_name":
        result["name"] = "read_files"
    elif damage == "result_before_call":
        value["messages"][1], value["messages"][2] = value["messages"][2], value["messages"][1]
    elif damage == "duplicate_call":
        value["messages"].insert(2, deepcopy(value["messages"][1]))
    elif damage == "duplicate_result":
        value["messages"].insert(3, deepcopy(value["messages"][2]))
    elif damage == "missing_result":
        value["messages"].pop(2)
    elif damage == "error":
        result["is_error"] = True
    elif damage == "echo_only":
        result["content"] = ""
        value["messages"][0]["content"][0]["text"] = DIGEST
    elif damage == "missing_marker":
        marker = ""
    elif damage == "no_final":
        value["messages"].pop()
    elif damage == "final_tool":
        value["messages"][-1]["content"].append(deepcopy(call))
    elif damage == "error_assistant":
        value["messages"][-1]["metadata"] = {"displayOnly": True, "displayRole": "error"}
    elif damage == "nonidle":
        value["row"]["status"] = "pending"
    else:
        value["messages"].insert(
            -1,
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "other", "name": "read_files", "input": {}}],
            },
        )
    assert not actual()["terminal_command_candidate"](value, COMMAND, DIGEST, "allow", marker, {})


class Terminal:
    def __init__(self, text=""):
        self._out = bytearray(text.encode())
        self._lock = threading.Lock()
        self.sent = []

    def send(self, value):
        self.sent.append(value)


def rejection_question():
    state = snapshot('ask-reject', status='running')
    state['messages'][2]['content'][0]['content'] = json.dumps({'error': actual()['USER_REJECTION']})
    question = {'type': 'tool_use', 'id': 'question-2', 'name': 'ask_question',
                'input': {'question': 'What should I do after the rejection?',
                          'options': ['Stop', 'Explain', 'Other']}}
    state['messages'][-1]['content'] = [question]
    return state, question


def question_panel(question):
    inputs = question['input']
    rows = ['Cline is asking a question', '', inputs['question'], '',
            '> ' + inputs['options'][0], *inputs['options'][1:], 'Type a response...', '', '']
    # Measured absolute-position rectangle; native scrollbar occupies one cell.
    return ('\x1b[?2026h' + ''.join(
        f'\x1b[{28 + i};1H' + text.ljust(118)
        + f'\x1b[{28 + i};119H' + ('█' if i in (2, 3, 4) else ' ')
        + f'\x1b[{28 + i};120H '
        for i, text in enumerate(rows)) + '\x1b[?2026l')


@pytest.mark.parametrize('prefix', ['', 'cd /workspace && '])
def test_actual_cline_exact_same_workspace_command_and_full_approval_F1560(prefix):
    helper = actual()
    spelling = prefix + COMMAND
    state = snapshot()
    state['messages'][1]['content'][0]['input']['commands'] = [spelling]
    assert helper['terminal_command_candidate'](state, COMMAND, DIGEST, 'allow', DIGEST, {})
    pending = messages('ask-allow', pending=True)
    pending[1]['content'][0]['input']['commands'] = [spelling]
    term, answered = Terminal(panel(spelling)), {}
    assert helper['answer_native_approval'](term, 0, pending, COMMAND, 'ask-allow', answered,
        child_running=lambda: False, marker_exists=lambda: False) == 'call-1'
    assert term.sent == [b'y']
    if prefix:
        term, answered = Terminal(panel(COMMAND)), {}
        assert helper['answer_native_approval'](term, 0, pending, COMMAND, 'ask-allow', answered,
            child_running=lambda: False, marker_exists=lambda: False) is None
        assert not term.sent


@pytest.mark.parametrize('commands', [
    [COMMAND + '; other'], [COMMAND, 'other'], ['cd /foreign && ' + COMMAND],
    ['cd /workspace && ' + COMMAND + '; other'], ['cd /workspace; ' + COMMAND],
    ['cd /workspace  && ' + COMMAND], [' cd /workspace && ' + COMMAND], [],
])
def test_actual_cline_same_workspace_equivalence_refuses_other_spelling_F1560(commands):
    assert not actual()['command_input']({'commands': commands}, COMMAND)


@pytest.mark.parametrize('damage', [None, 'stale', 'foreign_panel', 'unknown_panel',
    'wrong_tool', 'wrong_command', 'foreign_response', 'wrong_result', 'wrong_error',
    'wrong_order', 'duplicate', 'answered_question', 'foreign_last', 'options',
    'input_extra', 'level', 'already_clarified', 'child', 'marker', 'nested',
    'incomplete', 'duplicate_panel', 'overwrite', 'missing_coverage', 'unknown_csi'])
def test_actual_cline_source_correlated_rejection_question_explicit_answer_F1560(damage):
    helper = actual()
    state, question = rejection_question()
    raw = question_panel(question)
    responded, clarified, level, checkpoint = {'call-1': 'ask-reject'}, {}, 'ask-reject', 0
    if damage == 'stale':
        checkpoint = len(raw.encode())
    elif damage == 'foreign_panel':
        raw = raw.replace(question['input']['question'], 'Foreign question')
    elif damage == 'unknown_panel':
        raw = raw.replace('Stop', 'Stöp')
    elif damage == 'wrong_tool':
        question['name'] = 'read_files'
    elif damage == 'wrong_command':
        state['messages'][1]['content'][0]['input']['commands'] = ['foreign']
    elif damage == 'foreign_response':
        responded = {'foreign': 'ask-reject'}
    elif damage == 'wrong_result':
        state['messages'][2]['content'][0]['tool_use_id'] = 'foreign'
    elif damage == 'wrong_error':
        state['messages'][2]['content'][0]['content'] = json.dumps({'error': 'failed'})
    elif damage == 'wrong_order':
        state['messages'][2], state['messages'][3] = state['messages'][3], state['messages'][2]
    elif damage == 'duplicate':
        state['messages'][-1]['content'].append(deepcopy(question))
    elif damage == 'answered_question':
        state['messages'].append({'role': 'user', 'content': [{'type': 'tool_result',
            'tool_use_id': question['id'], 'name': 'ask_question', 'content': 'prior'}]})
    elif damage == 'foreign_last':
        state['messages'].append({'role': 'assistant', 'content': [{'type': 'text', 'text': 'other'}]})
    elif damage == 'options':
        question['input']['options'] = ['Stop', 'Stop', 'Other']
    elif damage == 'input_extra':
        question['input']['extra'] = True
    elif damage == 'level':
        level = 'ask-allow'
    elif damage == 'already_clarified':
        clarified = {'old': 'answer'}
    elif damage == 'nested':
        raw = '\x1b[?2026h' + raw
    elif damage == 'incomplete':
        raw = raw.removesuffix('\x1b[?2026l')
    elif damage == 'duplicate_panel':
        raw += raw
    elif damage == 'overwrite':
        raw += '\x1b[?2026h\x1b[30;1HForeign\x1b[?2026l'
    elif damage == 'missing_coverage':
        raw = raw.replace('\x1b[37;1H' + ' ' * 118, '')
    elif damage == 'unknown_csi':
        raw = raw.replace('\x1b[?2026l', '\x1b[2J\x1b[?2026l')
    term = Terminal(raw)
    checks = []
    def child():
        checks.append('child')
        return damage == 'child'
    def marker():
        checks.append('marker')
        return damage == 'marker'
    args = (term, checkpoint, state['messages'], COMMAND, level, responded, clarified)
    if damage in ('child', 'marker'):
        with pytest.raises(AssertionError):
            helper['answer_native_rejection_clarification'](*args, child_running=child, marker_exists=marker)
        assert not term.sent and not clarified
    elif damage is not None:
        assert helper['answer_native_rejection_clarification'](*args, child_running=child, marker_exists=marker) is None
        assert not term.sent and not checks
    else:
        assert helper['answer_native_rejection_clarification'](*args, child_running=child, marker_exists=marker) == question['id']
        assert checks == ['child', 'marker']
        assert term.sent == [helper['CLARIFICATION_RESPONSE'].encode('ascii') + b'\r']
        assert clarified == {question['id']: helper['CLARIFICATION_RESPONSE']}
        assert responded == {'call-1': 'ask-reject'}
        assert not helper['terminal_command_candidate'](state, COMMAND, DIGEST, level, '', responded)
        assert helper['answer_native_rejection_clarification'](*args, child_running=child, marker_exists=marker) is None
        assert len(term.sent) == 1


def test_actual_cline_clarification_requires_paired_idle_then_real_completion_F1560():
    helper = actual()
    state, question = rejection_question()
    responded = {'call-1': 'ask-reject'}
    def candidate(value):
        return helper['terminal_command_candidate'](value, COMMAND, DIGEST, 'ask-reject', '', responded)
    assert not candidate(state)
    state['row']['status'] = 'idle'
    assert not candidate(state)  # An unanswered question cannot become completion.
    state['messages'] += [
        {'role': 'user', 'content': [{'type': 'tool_result', 'name': 'ask_question',
          'tool_use_id': question['id'], 'content': helper['CLARIFICATION_RESPONSE']}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'NO-SHELL'}]},
    ]
    assert candidate(state)  # Explicitly synthetic continuation, not native evidence.
    after = deepcopy(state)
    assert not helper['terminal_command_outcome'](state, after, COMMAND, DIGEST, 'ask-reject', '', responded)
    after['row'].update(status='completed', exit_code=0, ended_at='2026-10-04T00:00:01Z')
    after['manifest'] = manifest(after['row'])
    assert helper['terminal_command_outcome'](state, after, COMMAND, DIGEST, 'ask-reject', '', responded)
    for field, bad in [('status', 'cancelled'), ('exit_code', 1), ('ended_at', None)]:
        broken = deepcopy(after)
        broken['row'][field] = bad
        assert not helper['terminal_command_outcome'](state, broken, COMMAND, DIGEST, 'ask-reject', '', responded)


def panel(command=COMMAND, tool="run_commands"):
    rows = [
        "Cline needs permission",
        "",
        "Approve tool call?",
        "",
        tool,
        "",
        "$ " + command,
        "",
        "[y] Approve  [n] Deny",
    ]
    return (
        "\x1b[?2026h"
        + "".join(f"\x1b[{27 + i};1H" + row.ljust(120) for i, row in enumerate(rows))
        + "\x1b[?2026l"
    )


@pytest.mark.parametrize("level", ["ask-allow", "ask-reject"])
def test_actual_cline_pending_call_full_fresh_panel_answers_exactly_once_F1560(level):
    helper = actual()
    term = Terminal("old unrelated output")
    checkpoint = helper["terminal_checkpoint"](term)
    term._out.extend(panel().encode())
    rows = messages(level, pending=True)
    answered = {}
    checks = []

    def child():
        checks.append("child")
        return False

    def marker():
        checks.append("marker")
        return False

    assert (
        helper["answer_native_approval"](
            term,
            checkpoint,
            rows,
            COMMAND,
            level,
            answered,
            child_running=child,
            marker_exists=marker,
        )
        == "call-1"
    )
    assert term.sent == [b"n" if level == "ask-reject" else b"y"] and checks == ["child", "marker"]
    assert (
        helper["answer_native_approval"](
            term,
            checkpoint,
            rows,
            COMMAND,
            level,
            answered,
            child_running=child,
            marker_exists=marker,
        )
        is None
    )
    assert len(term.sent) == 1


@pytest.mark.parametrize(
    "damage",
    [
        "stale",
        "echo",
        "wrong_command",
        "wrong_tool",
        "duplicate_panel",
        "no_footer",
        "different_call",
        "completed",
        "multiple_pending",
        "no_id",
        "child",
        "marker",
        "outside_ask",
        "cleared_panel",
    ],
)
def test_actual_cline_current_panel_and_native_call_refuse_wrong_approval_F1560(damage):
    helper = actual()
    term = Terminal()
    rows = messages("ask-allow", pending=True)
    level = "ask-allow"
    checkpoint = 0
    screen = panel()
    if damage == "stale":
        term._out.extend(screen.encode())
        checkpoint = helper["terminal_checkpoint"](term)
        screen = "fresh prompt echo"
    elif damage == "echo":
        screen = "User said Cline needs permission [y] Approve [n] Deny " + COMMAND
    elif damage == "wrong_command":
        screen = panel("foreign")
    elif damage == "wrong_tool":
        screen = panel(tool="read_files")
    elif damage == "duplicate_panel":
        screen += panel()
    elif damage == "no_footer":
        screen = screen.replace("[n] Deny", "")
    elif damage == "different_call":
        rows[1]["content"][0]["input"]["commands"] = ["foreign"]
    elif damage == "completed":
        rows = messages("ask-allow")
    elif damage == "multiple_pending":
        rows[1]["content"].append(
            {"type": "tool_use", "id": "other", "name": "read_files", "input": {}}
        )
    elif damage == "no_id":
        rows[1]["content"][0]["id"] = ""
    elif damage == "outside_ask":
        level = "allow"
    elif damage == "cleared_panel":
        screen += "\x1b[2Jnew ordinary prompt"
    term._out.extend(screen.encode())

    def operation():
        return helper["answer_native_approval"](
            term,
            checkpoint,
            rows,
            COMMAND,
            level,
            {},
            child_running=lambda: damage == "child",
            marker_exists=lambda: damage == "marker",
        )

    if damage in ("child", "marker", "outside_ask"):
        with pytest.raises(AssertionError):
            operation()
    else:
        assert operation() is None
    assert term.sent == []


@pytest.mark.parametrize(
    "damage", ["wrong_answer", "missing_answer", "error_absent", "digest_in_refusal", "marker"]
)
def test_actual_cline_rejection_requires_own_answer_and_native_error_no_execution_F1560(damage):
    value = snapshot("ask-reject")
    answered = respond("ask-reject")
    marker = ""
    if damage == "wrong_answer":
        answered = {"call-1": "ask-allow"}
    elif damage == "missing_answer":
        answered = {}
    elif damage == "error_absent":
        value["messages"][2]["content"][0]["is_error"] = False
    elif damage == "digest_in_refusal":
        value["messages"][2]["content"][0]["content"] = DIGEST
    else:
        marker = DIGEST
    assert not actual()["terminal_command_candidate"](
        value, COMMAND, DIGEST, "ask-reject", marker, answered
    )


@pytest.mark.parametrize(
    "attack", ["model", "provider", "document_session", "prompt", "cwd", "subagent"]
)
def test_actual_cline_native_model_message_reader_rejects_wrong_selection_F1560(tmp_path, attack):
    home = tmp_path / "home"
    path = write_native(home)
    if attack == "document_session":
        document = json.loads(path.read_text())
        document["sessionId"] = "foreign"
        path.write_text(json.dumps(document))
    else:
        column = {"prompt": "prompt", "cwd": "cwd", "subagent": "is_subagent"}.get(attack, attack)
        with sqlite3.connect(home / "sessions.db") as db:
            db.execute(f"UPDATE sessions SET {column}=?", ("foreign",))
    with pytest.raises(AssertionError):
        read_native(home)


@pytest.mark.parametrize(
    "attack",
    [
        "traversal",
        "message_link",
        "message_parent_link",
        "database_link",
        "database_parent_link",
        "home_link",
    ],
)
def test_actual_cline_native_conversation_refuses_owned_home_escape_F1560(tmp_path, attack):
    home = tmp_path / "home"
    path = write_native(home)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.messages.json"
    body = '{"sentinel":"must not read"}'
    sentinel.write_text(body)
    stored = "/home/coder/.cline/../outside/sentinel.messages.json"
    if attack == "message_link":
        path.unlink()
        path.symlink_to(sentinel)
        stored = "/home/coder/.cline/sessions/own/own.messages.json"
    elif attack == "message_parent_link":
        import shutil

        shutil.rmtree(path.parent)
        path.parent.symlink_to(outside, target_is_directory=True)
        stored = "/home/coder/.cline/sessions/own/sentinel.messages.json"
    with sqlite3.connect(home / "sessions.db") as db:
        db.execute("UPDATE sessions SET messages_path=?", (stored,))
    if attack == "database_link":
        (home / "sessions.db").rename(outside / "sessions.db")
        (home / "sessions.db").symlink_to(outside / "sessions.db")
    elif attack == "database_parent_link":
        (home / "sessions.db").rename(outside / "sessions.db")
        (home / "linked").symlink_to(outside, target_is_directory=True)
    elif attack == "home_link":
        link = tmp_path / "linked"
        link.symlink_to(home, target_is_directory=True)
        home = link
    with pytest.raises(AssertionError):
        read_native(home)
    assert sentinel.read_text() == body


def test_actual_cline_command_probe_writes_exact_digest_F1560(tmp_path):
    import hashlib
    import subprocess

    run = next(
        node
        for node in ast.parse(SOURCE.read_text()).body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_cline_actual_terminal_commands_and_private_file"
    )
    command = next(
        ast.literal_eval(node.value)
        for node in ast.walk(run)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "command" for t in node.targets)
    )
    body = "unpredictable fixture witness"
    (tmp_path / "commands-probe.txt").write_text(body)
    result = subprocess.run(
        [
            "sh",
            "-c",
            command.replace("/workspace/commands-probe.ran", str(tmp_path / "commands-probe.ran")),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    digest = hashlib.sha256(body.encode()).hexdigest()
    assert (
        result.stdout.strip() == digest
        and (tmp_path / "commands-probe.ran").read_text().strip() == digest
    )


@pytest.mark.parametrize("target", ["message", "database"])
def test_actual_cline_reader_refuses_fifo_before_blocking_F1560(tmp_path, target):
    import subprocess
    import sys

    home = tmp_path / "owned"
    home.mkdir()
    path = home / ("sessions.db" if target == "database" else "messages.json")
    os.mkfifo(path)
    script = """import ast,json,os,sqlite3,stat,sys
from pathlib import Path
source=Path(sys.argv[1]);names={'native_sessions','native_message_text','owned_native_path'}
functions=[n for n in ast.parse(source.read_text()).body if isinstance(n,ast.FunctionDef) and n.name in names]
exec(compile(ast.Module(body=functions,type_ignores=[]),str(source),'exec'))
try:
 if sys.argv[3]=='database': native_sessions(Path(sys.argv[2]))
 else: native_message_text(Path(sys.argv[2]),Path(sys.argv[2])/'messages.json')
except AssertionError:
 print('nonregular-refused')
else: raise AssertionError('Nonregular reader accepted FIFO')
"""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, str(SOURCE), str(home), target],
            capture_output=True,
            text=True,
            timeout=3,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "nonregular-refused"
    finally:
        path.unlink()


@pytest.mark.parametrize("native_exit", [0, 1, "writer-error"])
def test_actual_cline_live_flow_awaits_native_quit_before_runner_cleanup_F1560(
    tmp_path, monkeypatch, native_exit
):
    """Actual live function + SQLite, scripted lifecycle only (not native proof)."""
    import asyncio
    from types import SimpleNamespace

    evidence_error = native_exit == "writer-error"
    if evidence_error:
        native_exit = 1
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-evidence-key")
    helpers = actual()
    node = next(
        n
        for n in ast.parse(SOURCE.read_text()).body
        if isinstance(n, ast.FunctionDef)
        and n.name == "test_cline_actual_terminal_commands_and_private_file"
    )
    node.decorator_list = []
    command = next(
        ast.literal_eval(n.value)
        for n in ast.walk(node)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "command" for t in n.targets)
    )
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    order, terms = [], {}

    class Process:
        rc = None

        def poll(self):
            return self.rc

    term = Terminal()
    term.proc = Process()
    term.stop = lambda: order.append("own-client-close")
    env_obj = object()
    native = SimpleNamespace(
        CLINE_HOME="/home/coder/.cline", key_settings_host_dir=lambda: str(tmp_path / "key")
    )

    async def prepare(*args, **kwargs):
        return SimpleNamespace(env_obj=env_obj)

    native.prepare = prepare

    async def runner(cfg, *, pointer):
        # The leg ends when its terminal's program exits.
        assert cfg["backend"] == "cline"
        await native.prepare()
        terms[pointer] = term
        while term.proc.poll() is None:
            await asyncio.sleep(0.01)
        assert term.proc.poll() == native_exit
        assert "native-quit" in order
        order.append("runner-finally")

    def selected_runner(backend, surface):
        assert (backend, surface) == ("cline", "terminal")
        return runner

    def type_line(terminal, text):
        assert terminal is term
        if text == PROMPT:
            path = write_native(home)
            document = json.loads(path.read_text())
            document["messages"][1]["content"][0]["input"]["commands"] = [command]
            path.write_text(json.dumps(document))
            (workspace / "commands-probe.ran").write_text(DIGEST)
            order.append("submitted")
        else:
            assert text == "/quit"
            assert "runner-finally" not in order
            with sqlite3.connect(home / "sessions.db") as db:
                db.execute(
                    "UPDATE sessions SET status='completed',ended_at='2026-10-04T00:00:01Z',exit_code=0"
                )
            (home / "sessions/own/own.json").write_text(json.dumps(manifest(row("completed"))))
            term.proc.rc = native_exit
            order.append("native-quit")

    helpers.update(
        {
            "asyncio": asyncio,
            "pytest": pytest,
            "PINS": {"cline": "3.0.73", "mcp-remote": "0.15.2"},
            "time": __import__("time"),
            "cline": native,
            "surface_runner": selected_runner,
            "model_for": lambda p: MODEL,
            "bind_model_account": lambda a: None,
            "model_account": lambda *a, **k: None,
            "conformance": SimpleNamespace(
                _write_probe=lambda: DIGEST, _probe_prompt=lambda c: PROMPT
            ),
            "harness_homes": SimpleNamespace(harness_home=lambda h: str(home)),
            "_host_terminals": lambda *a: terms,
            "_up_to_prompt": lambda *a: (None, False),
            "_type_line": type_line,
            "assert_private": lambda p: order.append("private-scan"),
            "gen": SimpleNamespace(run_path=lambda p: p, workspace_path=lambda: str(workspace)),
            "run_on_test_loop": asyncio.run,
        }
    )
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), helpers)

    if evidence_error:

        def failed_writer(*args, **kwargs):
            order.append("evidence-failure")
            raise OSError("synthetic evidence write failure")

        helpers["capture_terminal_evidence"] = failed_writer

    def invocation():
        return helpers[node.name](SimpleNamespace(), monkeypatch, "allow", tmp_path)

    if evidence_error:
        with pytest.raises(OSError, match="evidence write failure"):
            invocation()
    elif native_exit:
        with pytest.raises(AssertionError, match="exit normally"):
            invocation()
    else:
        invocation()
    assert (
        order.index("native-quit") < order.index("runner-finally") < order.index("own-client-close")
    )
    if evidence_error:
        assert order.index("evidence-failure") < order.index("runner-finally")
    else:
        assert (tmp_path / "cline-terminal-0.json").is_file(), "Finally evidence was not retained"
    assert term.sent == [], "Normal quit cannot be replaced by a client interrupt"


@pytest.mark.parametrize(
    "stored",
    [
        '<user_input mode="act">' + PROMPT + "</user_input>",
        PROMPT,
        '<user_input mode="plan">' + PROMPT + "</user_input>",
        '<user_input mode="act">different</user_input>',
        '<user_input mode="act">' + PROMPT + "</user_input>extra",
    ],
)
def test_actual_sqlite_cline_exact_native_act_prompt_wrapper_F1560(tmp_path, stored):
    home = tmp_path / "home"
    write_native(home, "deny")
    with sqlite3.connect(home / "sessions.db") as db:
        db.execute("UPDATE sessions SET prompt=?", (stored,))
    if stored == '<user_input mode="act">' + PROMPT + "</user_input>":
        assert read_native(home)["row"]["prompt"] == stored
    else:
        with pytest.raises(AssertionError, match="Native prompt differs"):
            read_native(home)


def unavailable_command_error():
    return json.dumps(
        {
            "error": "Tool call run_commands was rejected before execution: "
            "AI_NoSuchToolError: Model tried to call unavailable tool 'run_commands'. "
            "Available tools: read_files, search_codebase, editor."
        }
    )


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "success",
        "generic_error",
        "available",
        "missing",
        "wrong_name",
        "wrong_id",
        "digest",
        "marker",
        "answered",
        "not_final",
        "not_idle",
    ],
)
def test_cline_deny_rejected_unavailable_command_never_authorizes_execution_F1560(damage):
    state = snapshot("deny")
    call = {
        "type": "tool_use",
        "id": "rejected-call",
        "name": "run_commands",
        "input": {"commands": ["cd /workspace && " + COMMAND]},
    }
    result = {
        "type": "tool_result",
        "tool_use_id": "rejected-call",
        "name": "run_commands",
        "is_error": True,
        "content": unavailable_command_error(),
    }
    state["messages"][1:1] = [
        {"role": "assistant", "content": [call]},
        {"role": "user", "content": [result]},
    ]
    marker, answers = "", {}
    if damage == "success":
        result["is_error"] = False
    elif damage == "generic_error":
        result["content"] = json.dumps({"error": "execution failed"})
    elif damage == "available":
        result["content"] = unavailable_command_error().replace("editor.", "editor, run_commands.")
    elif damage == "missing":
        state["messages"].pop(2)
    elif damage == "wrong_name":
        result["name"] = "other"
    elif damage == "wrong_id":
        result["tool_use_id"] = "other"
    elif damage == "digest":
        result["content"] = DIGEST
    elif damage == "marker":
        marker = DIGEST
    elif damage == "answered":
        answers = {"rejected-call": "ask-allow"}
    elif damage == "not_final":
        state["messages"][-1]["content"][0]["text"] = "other"
    elif damage == "not_idle":
        state["row"]["status"] = "running"
    assert actual()["terminal_command_candidate"](
        state, COMMAND, DIGEST, "deny", marker, answers
    ) is (damage is None)


@pytest.mark.parametrize("damage", [None, "existing", "symlink", "raw_bound", "snapshot_bound"])
def test_actual_cline_terminal_private_witness_bytes_bounds_and_exclusive_write_F1560(
    tmp_path, damage
):
    import base64

    key = "synthetic-terminal-private-key"
    term = Terminal("\x1b[2Jcurrent native panel " + key + "\n")
    state = snapshot("ask-reject")
    state["messages"][0]["content"][0]["text"] = key
    path = tmp_path / "evidence.json"
    if damage == "existing":
        path.write_text("preserved")
    elif damage == "symlink":
        target = tmp_path / "victim"
        target.write_text("preserved")
        path.symlink_to(target)
    elif damage == "raw_bound":
        term._out = bytearray(b"x" * (2 * 1024 * 1024 + 1))
    elif damage == "snapshot_bound":
        state["messages"][0]["content"][0]["text"] = "x" * (4 * 1024 * 1024)
    kwargs = dict(checkpoint=7, level="ask-reject", responded={}, clarified={}, snapshot=state, selected_key=key)
    if damage is not None:
        with pytest.raises((FileExistsError, AssertionError)):
            actual()["capture_terminal_evidence"](path, term, **kwargs)
        if damage == "existing":
            assert path.read_text() == "preserved"
        elif damage == "symlink":
            assert target.read_text() == "preserved"
        else:
            assert not path.exists()
    else:
        actual()["capture_terminal_evidence"](path, term, **kwargs)
        value = json.loads(path.read_bytes())
        assert path.stat().st_mode & 0o777 == 0o600
        assert key.encode() not in path.read_bytes()
        raw = base64.b64decode(value["raw_ansi_base64"], validate=True)
        assert key.encode() not in raw and raw.startswith(b"\x1b[2Jcurrent native panel ")
        assert value["checkpoint"] == 7 and value["native_snapshot_present"] is True
        assert value["native_snapshot"]["row"] == state["row"]
        assert key not in json.dumps(value["native_snapshot"])


# Exact sanitized native approval rectangle: observed CUP/SGR/padding only.
ACTUAL_CLINE_APPROVAL_RECTANGLE = b"\x1b[?2026h\x1b[27;1H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[27;2H\x1b[38;5;111m\x1b[48;5;234mCline needs permission\x1b[0m\x1b[27;24H\x1b[38;5;15m\x1b[48;5;234m                                                                                                 \x1b[28;1H\x1b[38;5;15m\x1b[48;5;234m                                                                                                                        \x1b[29;1H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[29;2H\x1b[38;5;222m\x1b[48;5;234mApprove tool call?\x1b[0m\x1b[29;20H\x1b[38;5;15m\x1b[48;5;234m                                                                                                     \x1b[30;1H\x1b[38;5;15m\x1b[48;5;234m                                                                                                                        \x1b[31;1H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[31;2H\x1b[38;5;111m\x1b[48;5;234mrun_commands\x1b[0m\x1b[31;14H\x1b[38;5;15m\x1b[48;5;234m                                                                                                           \x1b[32;1H\x1b[38;5;15m\x1b[48;5;234m                                                                                                                        \x1b[33;1H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[33;2H\x1b[38;5;8m\x1b[48;5;234m  $ sha256sum commands-probe.txt | cut -d ' ' -f1 | tee /workspace/commands-probe.ran\x1b[0m\x1b[33;87H\x1b[38;5;15m\x1b[48;5;234m                                  \x1b[34;1H\x1b[38;5;15m\x1b[48;5;234m                                                                                                                        \x1b[35;1H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[35;2H\x1b[38;5;15m\x1b[48;5;111m \x1b[0m\x1b[35;3H\x1b[38;5;0m\x1b[48;5;111m[y] Approve\x1b[0m\x1b[35;14H\x1b[38;5;15m\x1b[48;5;111m \x1b[0m\x1b[35;15H\x1b[38;5;15m\x1b[48;5;234m  [n] Deny                                                                                                \x1b[?2026l"


def test_actual_cline_positioned_native_approval_witness_F1560():
    command = "sha256sum commands-probe.txt | cut -d ' ' -f1 | tee /workspace/commands-probe.ran"
    term = Terminal()
    term._out.extend(ACTUAL_CLINE_APPROVAL_RECTANGLE)
    assert actual()["fresh_approval_panel"](term, 0, command)


@pytest.mark.parametrize("damage", ["outer_begin", "unicode_wrap", "ascii_after_unicode"])
def test_cline_positioned_ambiguity_cannot_authorize_native_answer_F1560(damage):
    command = "sha256sum commands-probe.txt | cut -d ' ' -f1 | tee /workspace/commands-probe.ran"
    raw = ACTUAL_CLINE_APPROVAL_RECTANGLE
    if damage == "outer_begin":
        raw = b"\x1b[?2026h" + raw
    else:
        raw += b"\x1b[?2026h\x1b[26;1H" + ("界" * 240).encode()
        if damage == "ascii_after_unicode":
            raw += b"\x1b[0m" + b"x" * 240
        raw += b"\x1b[?2026l"
    term = Terminal()
    term._out.extend(raw)
    rows = messages("ask-allow", pending=True)
    rows[-1]["content"][0]["input"] = {"commands": [command]}
    responded = {}
    answer = actual()["answer_native_approval"](
        term, 0, rows, command, "ask-allow", responded,
        child_running=lambda: False, marker_exists=lambda: False,
    )
    assert answer is None and term.sent == [] and responded == {}


def test_cline_positioned_bounded_outside_unicode_requires_new_absolute_cursor_F1560():
    command = "sha256sum commands-probe.txt | cut -d ' ' -f1 | tee /workspace/commands-probe.ran"
    raw = ACTUAL_CLINE_APPROVAL_RECTANGLE
    # Both short outside spans occur in the retained native witness. No width
    # is assumed; a byte-count upper bound proves they cannot leave their row.
    raw += b"\x1b[?2026h\x1b[7;2H" + "界".encode()
    raw += b"\x1b[38;20H" + "界".encode() + b"\x1b[?2026l"
    term = Terminal()
    term._out.extend(raw)
    assert actual()["fresh_approval_panel"](term, 0, command)
    term._out.extend(b"\x1b[?2026h\x1b[26;1H" + "界".encode()
                     + b"\x1b[0m" + b"x" * 120 + b"\x1b[?2026l")
    assert not actual()["fresh_approval_panel"](term, 0, command)


@pytest.mark.parametrize(
    "damage",
    [
        "erase",
        "overwrite",
        "incomplete",
        "nested",
        "duplicate",
        "wrong_command",
        "wrong_tool",
        "overflow",
        "wrap",
        "partial_row",
        "unsupported",
        "unicode",
        "stale",
    ],
)
def test_cline_positioned_panel_refuses_incomplete_ambiguous_or_overwritten_screen_F1560(damage):
    command = "sha256sum commands-probe.txt | cut -d ' ' -f1 | tee /workspace/commands-probe.ran"
    raw = ACTUAL_CLINE_APPROVAL_RECTANGLE
    checkpoint = 0
    if damage == "erase":
        raw += b"\x1b[?2026h\x1b[27;2H\x1b[2K\x1b[?2026l"
    elif damage == "overwrite":
        raw += b"\x1b[?2026h\x1b[27;2Hxxxxxxxxxxxxxxxxxxxxxx\x1b[?2026l"
    elif damage == "incomplete":
        raw = raw[:-8]
    elif damage == "nested":
        raw = raw.replace(b"Cline needs permission", b"\x1b[?2026hCline needs permission")
    elif damage == "duplicate":
        raw += raw
    elif damage == "wrong_command":
        command += " extra"
    elif damage == "wrong_tool":
        raw = raw.replace(b"run_commands", b"read_files  ")
    elif damage == "overflow":
        raw = raw.replace(b"\x1b[27;2H", b"\x1b[27;119H")
    elif damage == "wrap":
        raw = raw.replace(b"Cline needs permission", b"Cline needs\npermission")
    elif damage == "partial_row":
        raw = raw.replace(b"\x1b[28;1H", b"\x1b[28;2H")
    elif damage == "unsupported":
        raw += b"\x1b[?2026h\x1b[1A\x1b[?2026l"
    elif damage == "unicode":
        raw += b"\x1b[?2026h\x1b[27;2H" + "界".encode() + b"\x1b[?2026l"
    elif damage == "stale":
        checkpoint = len(raw)
    term = Terminal()
    term._out.extend(raw)
    assert not actual()["fresh_approval_panel"](term, checkpoint, command)


@pytest.mark.parametrize(
    "damage", [None, "missing_id", "second_pending", "completed", "child", "marker"]
)
def test_cline_actual_positioned_panel_keeps_pending_identity_and_before_answer_gate_F1560(damage):
    command = "sha256sum commands-probe.txt | cut -d ' ' -f1 | tee /workspace/commands-probe.ran"
    term = Terminal()
    term._out.extend(ACTUAL_CLINE_APPROVAL_RECTANGLE)
    rows = messages("ask-reject", pending=True)
    call = rows[-1]["content"][0]
    call["input"] = {"commands": [command]}
    if damage == "missing_id":
        call.pop("id")
    elif damage == "second_pending":
        other = deepcopy(rows[-1])
        other["content"][0]["id"] = "other"
        rows.append(other)
    elif damage == "completed":
        rows.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call-1", "name": "run_commands"}
                ],
            }
        )
    responded = {}
    checks = []

    def child():
        checks.append("child")
        return damage == "child"

    def marker():
        checks.append("marker")
        return damage == "marker"

    def answer():
        return actual()["answer_native_approval"](
            term,
            0,
            rows,
            command,
            "ask-reject",
            responded,
            child_running=child,
            marker_exists=marker,
        )

    if damage in ("child", "marker"):
        with pytest.raises(AssertionError):
            answer()
    else:
        assert answer() == ("call-1" if damage is None else None)
    assert term.sent == ([b"n"] if damage is None else [])
    if damage is None:
        assert checks == ["child", "marker"] and responded == {"call-1": "ask-reject"}
        assert answer() is None and term.sent == [b"n"]


# Public installed native SHA b28335dfe19c6d11cc69dd3872d800e60e1b7f0c22ca1c4aca09540d516bf129;
# U5 byte118260644 and f7 callback byte118266702, isolated source only.
NATIVE_QUESTION_INPUT_CONTROL = 'const vm = require(\'node:vm\');\nconst u5=\'function U5($){if($.ctrl||$.meta||$.super||$.hyper||!$.sequence)return null;let X=$.sequence.charCodeAt(0);if(X<32||X===127)return null;return $.sequence}\';\nconst callback=\'(h)=>{let k=K.current===U;if(h.name==="escape"){if(k&&F.current){L("");return}w(null);return}if(k&&h.name==="backspace"){L(H5(F.current));return}if(k&&h.name==="delete"){L("");return}if(h.name==="return"||h.name==="enter"){if(k){let g=F.current.trim();if(g){w(g);return}W(!0);return}w(X.options[K.current]??"");return}if(h.name==="up"||h.ctrl&&h.name==="p"){let g=K.current<=0?A-1:K.current-1;B(g);return}if(h.name==="down"||h.ctrl&&h.name==="n"){let g=K.current>=A-1?0:K.current+1;B(g);return}if(!k&&h.name>="1"&&h.name<="9"){let g=Number.parseInt(h.name,10)-1,M=X.options[g];if(M){w(M);return}}let c=U5(h);if(k&&c!==null){L(`${F.current}${c}`);return}if(c!==null)L(c),B(U)}\';\nfunction run(text,escape=false){\n const context={answer:[],X:{options:[\'option0\',\'option1\',\'option2\']},K:{current:0},F:{current:\'\'},U:3,A:4};\n context.L=s=>context.F.current=s;context.B=s=>context.K.current=s;context.w=s=>context.answer.push(s);context.W=()=>{};context.H5=()=>{throw Error(\'unused unsupported backspace\');};\n const call=vm.runInNewContext(`${u5}\\n(${callback})`,context);\n if(escape)call({name:\'escape\',sequence:\'\\x1b\'});\n else {for(const c of text)call({name:c,sequence:c});call({name:\'return\',sequence:\'\\r\'});}\n return context.answer;\n}\nconst explicit=\'Do not run any command. Report NO-SHELL and stop.\';\nif(JSON.stringify(run(explicit))!==JSON.stringify([explicit]))throw Error(\'Explicit custom user response differs\');\nif(JSON.stringify(run(\'/quit\'))!==JSON.stringify([\'/quit\']))throw Error(\'Pending question quit behavior differs\');\nif(JSON.stringify(run(\'\',true))!==JSON.stringify([null]))throw Error(\'Dismissal differs\');\nconsole.log(\'Actual pinned printable-key decoder and question callback: explicit ASCII custom response resolves exactly; /quit is a question answer; Escape resolves null. PASS (offline source isolation, no native UI execution)\');\n'

def test_actual_pinned_cline_question_custom_input_source_control_F1560():
    import subprocess
    result = subprocess.run(["node", "-e", NATIVE_QUESTION_INPUT_CONTROL],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "PASS (offline source isolation, no native UI execution)" in result.stdout


# Exact scrubbed native failed ask-reject witness SHA4e2c4861..., pinned Cline3.0.65.
ACTUAL_CLINE_TWO_OPTION_PANEL = b"\x1b[?2026h\x1b[?25l\x1b[15;2H\x1b[38;5;111m\x1b[49m*\x1b[0m\x1b[17;2H\x1b[38;5;8m\x1b[49m\xe2\xa0\x8b\x1b[0m\x1b[17;4H\x1b[38;5;111m\x1b[49m\x1b[1mask_question(\x1b[0m\x1b[17;17H\x1b[38;5;15m\x1b[49mThe\x1b[0m\x1b[17;21H\x1b[38;5;15m\x1b[49msandbox\x1b[0m\x1b[17;29H\x1b[38;5;15m\x1b[49mblocked\x1b[0m\x1b[17;37H\x1b[38;5;15m\x1b[49m`sha256sum\x1b[0m\x1b[17;48H\x1b[38;5;15m\x1b[49mcommands-probe.txt\x1b[0m\x1b[17;67H\x1b[38;5;15m\x1b[49m|\x1b[0m\x1b[17;69H\x1b[38;5;15m\x1b[49mcut\x1b[0m\x1b[17;73H\x1b[38;5;15m\x1b[49m-d\x1b[0m\x1b[17;76H\x1b[38;5;15m\x1b[49m'...\x1b[0m\x1b[17;80H\x1b[38;5;111m\x1b[49m\x1b[1m)\x1b[0m\x1b[27;1H\x1b[38;5;15m\x1b[48;5;234m                                                                                                                        \x1b[28;1H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[28;2H\x1b[38;5;111m\x1b[48;5;234mCline is asking a question\x1b[0m\x1b[28;28H\x1b[38;5;15m\x1b[48;5;234m                                                                                             \x1b[29;1H\x1b[38;5;15m\x1b[48;5;234m                                                                                                                        \x1b[30;1H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[30;2H\x1b[38;5;255m\x1b[48;5;234mThe sandbox blocked `sha256sum commands-probe.txt | cut -d ' ' -f1 | tee /workspace/commands-probe.ran`. May I run it\x1b[0m\x1b[30;119H\x1b[38;5;247m\x1b[48;5;235m\xe2\x96\x88\x1b[0m\x1b[30;120H\x1b[38;5;15m\x1b[48;5;234m \x1b[31;1H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[31;2H\x1b[38;5;255m\x1b[48;5;234moutside the sandbox?\x1b[0m\x1b[31;22H\x1b[38;5;15m\x1b[48;5;234m                                                                                                 \x1b[0m\x1b[31;119H\x1b[38;5;247m\x1b[48;5;235m\xe2\x96\x88\x1b[0m\x1b[31;120H\x1b[38;5;15m\x1b[48;5;234m \x1b[32;1H\x1b[38;5;15m\x1b[48;5;234m                                                                                                                      \x1b[0m\x1b[32;119H\x1b[38;5;247m\x1b[48;5;235m\xe2\x96\x88\x1b[0m\x1b[32;120H\x1b[38;5;15m\x1b[48;5;234m \x1b[33;1H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[33;2H\x1b[38;5;15m\x1b[48;5;111m \x1b[0m\x1b[33;3H\x1b[38;5;0m\x1b[48;5;111m>\x1b[0m\x1b[33;4H\x1b[38;5;15m\x1b[48;5;111m \x1b[0m\x1b[33;5H\x1b[38;5;0m\x1b[48;5;111mYes, run it outside the sandbox.\x1b[0m\x1b[33;37H\x1b[38;5;15m\x1b[48;5;111m                                                                                  \x1b[0m\x1b[33;119H\x1b[38;5;15m\x1b[48;5;235m \x1b[0m\x1b[33;120H\x1b[38;5;15m\x1b[48;5;234m \x1b[34;1H\x1b[38;5;15m\x1b[48;5;234m  \x1b[0m\x1b[34;3H\x1b[38;5;8m\x1b[48;5;234m \x1b[0m\x1b[34;4H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[34;5H\x1b[38;5;255m\x1b[48;5;234mNo, skip it.\x1b[0m\x1b[34;17H\x1b[38;5;15m\x1b[48;5;234m                                                                                                      \x1b[0m\x1b[34;119H\x1b[38;5;15m\x1b[48;5;235m \x1b[0m\x1b[34;120H\x1b[38;5;15m\x1b[48;5;234m \x1b[35;1H\x1b[38;5;15m\x1b[48;5;234m                                                                                                                      \x1b[0m\x1b[35;119H\x1b[38;5;15m\x1b[48;5;235m \x1b[0m\x1b[35;120H\x1b[38;5;15m\x1b[48;5;234m \x1b[36;1H\x1b[38;5;15m\x1b[48;5;234m                                                                                                                        \x1b[37;1H\x1b[38;5;15m\x1b[49m                                                                                                                        \x1b[38;30H\x1b[38;5;8m\x1b[49m834\x1b[0m\x1b[0m\x1b[36;3H\x1b[?25h\x1b[?2026l\x1b[?2026h\x1b[?25l\x1b[30;119H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[31;119H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[32;119H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[33;119H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[34;119H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[35;3H\x1b[38;5;8m\x1b[48;5;234m \x1b[0m\x1b[35;5H\x1b[38;5;102m\x1b[48;5;234mType a response...\x1b[0m\x1b[35;119H\x1b[38;5;15m\x1b[48;5;234m \x1b[0m\x1b[0m\x1b[?2026l"
ACTUAL_CLINE_TWO_OPTION_MESSAGES = [{'id': '204c255b-12c5-491c-94e5-008c62bbb805', 'role': 'user', 'content': [{'type': 'text', 'text': '<user_input mode="act">Use your shell tool to run exactly this command: sha256sum commands-probe.txt | cut -d \' \' -f1 | tee /workspace/commands-probe.ran\nIf a sandbox blocks it, ask for permission to run it outside the sandbox.\nThen reply with only the command\'s output. If you cannot run it, reply NO-SHELL.</user_input>'}], 'ts': 1791156002558}, {'id': 'msg_p343mh5F', 'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'The user wants me to run a specific shell command. Let me execute it.'}, {'type': 'tool_use', 'id': 'call_3934c68cd8674976988f4799', 'name': 'run_commands', 'input': {'commands': ["sha256sum commands-probe.txt | cut -d ' ' -f1 | tee /workspace/commands-probe.ran"]}}], 'ts': 1791156003809, 'modelInfo': {'id': 'deepseek/deepseek-v4-flash', 'provider': 'openrouter'}, 'metrics': {'inputTokens': 5703, 'outputTokens': 90, 'cacheReadTokens': 0, 'cacheWriteTokens': 0, 'cost': 0.0002429472}}, {'id': 'msg_ZRSqI4fC', 'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call_3934c68cd8674976988f4799', 'name': 'run_commands', 'content': '{"error":"This tool call was rejected by the user and not executed. -- NOT a tool or system failure. Clarify with user before proceeding."}', 'is_error': True}], 'ts': 1791156003956}, {'id': 'msg_wB26qm2p', 'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'The sandbox blocked the command execution. Per the user\'s instruction: "If a sandbox blocks it, ask for permission to run it outside the sandbox." I need to ask for permission.'}, {'type': 'text', 'text': 'The sandbox blocked the command. May I run it outside the sandbox?\n\n'}, {'type': 'tool_use', 'id': 'call_f3b4b128d74c422f99566bfe', 'name': 'ask_question', 'input': {'question': "The sandbox blocked `sha256sum commands-probe.txt | cut -d ' ' -f1 | tee /workspace/commands-probe.ran`. May I run it outside the sandbox?", 'options': ['Yes, run it outside the sandbox.', 'No, skip it.']}}], 'ts': 1791156007080, 'modelInfo': {'id': 'deepseek/deepseek-v4-flash', 'provider': 'openrouter'}, 'metrics': {'inputTokens': 5834, 'outputTokens': 173, 'cacheReadTokens': 5632, 'cacheWriteTokens': 0, 'cost': 0.0003521216000000001}}]


@pytest.mark.parametrize('damage', [None, 'question', 'option', 'count', 'split',
    'unknown_part', 'unknown_input', 'foreign_command', 'foreign_response',
    'foreign_result', 'wrong_error', 'stale', 'child', 'marker'])
def test_actual_cline_two_option_wrapped_question_requires_full_native_pair_F1560(damage):
    helper = actual()
    messages = deepcopy(ACTUAL_CLINE_TWO_OPTION_MESSAGES)
    raw = ACTUAL_CLINE_TWO_OPTION_PANEL.decode('utf8')
    command = messages[1]['content'][-1]
    requested_command = command['input']['commands'][0]
    question = messages[-1]['content'][-1]
    responded = {command['id']: 'ask-reject'}
    if damage == 'question':
        question['input']['question'] += ' foreign'
    elif damage == 'option':
        question['input']['options'][1] = 'Different option'
    elif damage == 'count':
        question['input']['options'].append('Other')
    elif damage == 'split':
        raw = raw.replace('outside the sandbox?', 'outside  the sandbox?')
    elif damage == 'unknown_part':
        question['name'] = 'run_commands'
    elif damage == 'unknown_input':
        question['input']['extra'] = True
    elif damage == 'foreign_command':
        command['input']['commands'][0] += ' && true'
    elif damage == 'foreign_response':
        responded = {'foreign': 'ask-reject'}
    elif damage == 'foreign_result':
        messages[2]['content'][0]['tool_use_id'] = 'foreign'
    elif damage == 'wrong_error':
        messages[2]['content'][0]['content'] = '{"error":"tool failure"}'
    term = Terminal(raw)
    clarified = {}
    if damage in ('child', 'marker'):
        with pytest.raises(AssertionError):
            helper['answer_native_rejection_clarification'](
                term, 0, messages, requested_command, 'ask-reject', responded, clarified,
                child_running=lambda: damage == 'child', marker_exists=lambda: damage == 'marker')
        assert term.sent == [] and clarified == {}
        return
    result = helper['answer_native_rejection_clarification'](
        term, len(term._out) if damage == 'stale' else 0,
        messages, requested_command, 'ask-reject', responded, clarified,
        child_running=lambda: damage == 'child', marker_exists=lambda: damage == 'marker')
    if damage is None:
        assert result == question['id']
        assert term.sent == [helper['CLARIFICATION_RESPONSE'].encode('ascii') + b'\r']
        assert clarified == {question['id']: helper['CLARIFICATION_RESPONSE']}
        assert helper['answer_native_rejection_clarification'](
            term, 0, messages, requested_command, 'ask-reject', responded, clarified,
            child_running=lambda: False, marker_exists=lambda: False) is None
        assert len(term.sent) == 1
    else:
        assert result is None and term.sent == [] and clarified == {}
