# SPDX-License-Identifier: MIT
"""Pinned Cline native grant/credential gates; no echoed-text approval automation."""

import asyncio
from datetime import datetime
import json
import os
import re
from pathlib import Path
import sqlite3
import stat
import time

import gen
import pytest
from _agent_shared import harness_homes, surface_runner
from _agent_shared.harnesses.cline import backend as cline
from tests_live.conftest import (
    bind_model_account,
    has_key,
    model_account,
    model_for,
    requires_docker,
    run_on_test_loop,
)
from tests_live import test_live_acp_conformance as conformance
from tests_live._host_terminal import _host_terminals, _type_line
from tests_live._host_terminal import _up_to_prompt

PINS = {"cline": "3.0.73", "mcp-remote": "0.15.2"}
pytestmark = [
    requires_docker,
    pytest.mark.usefixtures("one_loop_per_test"),
    pytest.mark.skipif(not has_key("openrouter"), reason="needs authorized OPENROUTER_API_KEY"),
]


def command_input(value, command):
    # The real native session is already pinned to cwd/workspace_root=/workspace.
    # Admit only the measured redundant same-cwd prefix, never parse shell text.
    return isinstance(value, dict) and value.get("commands") in (
        [command], ["cd /workspace && " + command]
    )


def native_tool_records(messages):
    """Persisted native tool-use/result IDs, before any UI projection."""
    calls, results = [], []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            return None
        for part in message["content"]:
            if not isinstance(part, dict):
                return None
            if part.get("type") == "tool_use":
                if message.get("role") != "assistant" or not part.get("id") or not part.get("name"):
                    return None
                calls.append((index, part))
            elif part.get("type") == "tool_result":
                if (
                    message.get("role") != "user"
                    or not part.get("tool_use_id")
                    or not part.get("name")
                ):
                    return None
                results.append((index, part))
    ids = [part["id"] for _, part in calls]
    if len(set(ids)) != len(ids):
        return None
    result_ids = [part["tool_use_id"] for _, part in results]
    if len(set(result_ids)) != len(result_ids):
        return None
    for index, part in results:
        owners = [
            (i, p) for i, p in calls if p["id"] == part["tool_use_id"] and p["name"] == part["name"]
        ]
        if len(owners) != 1 or owners[0][0] >= index:
            return None
    return calls, results


def pending_command_call(messages, command):
    records = native_tool_records(messages)
    if records is None:
        return None
    calls, results = records
    finished = {part["tool_use_id"] for _, part in results}
    pending = [part for _, part in calls if part["id"] not in finished]
    if (
        len(pending) != 1
        or pending[0]["name"] != "run_commands"
        or not command_input(pending[0].get("input"), command)
    ):
        return None
    return pending[0]


def capture_terminal_evidence(path, term, *, checkpoint, level, responded, clarified, snapshot, selected_key):
    """Private bounded finally witness; no config/account files or screen guesses."""
    import base64
    from _agent_shared import secret_hygiene

    secret_hygiene.remember(selected_key)
    with term._lock:
        raw = bytes(term._out)
    assert len(raw) <= 2 * 1024 * 1024, "Raw native terminal witness exceeded its bound"
    scrubbed = secret_hygiene.scrub_bytes(raw)
    value = secret_hygiene.scrub_object(
        {
            "level": level,
            "checkpoint": checkpoint,
            "raw_bytes": len(raw),
            "raw_ansi_base64": base64.b64encode(scrubbed).decode("ascii"),
            "responded": responded,
            "clarified": clarified,
            "native_snapshot": snapshot,
            "native_snapshot_present": snapshot is not None,
        }
    )
    body = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode()
    assert len(body) <= 4 * 1024 * 1024, "Native terminal evidence exceeded its bound"
    assert selected_key and selected_key.encode() not in scrubbed
    assert selected_key.encode() not in body, "Selected key in terminal evidence"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(body)


def terminal_checkpoint(term):
    with term._lock:
        return len(term._out)


def positioned_approval_rows(raw):
    """Observed native CUP/SGR ASCII rectangle; deliberately not a VT emulator."""
    begin, end = b"\x1b[?2026h", b"\x1b[?2026l"
    title = b"Cline needs permission"
    if raw.count(title) != 1:
        return None
    first = None
    for marker in re.finditer(rb"\x1b\[\?2026[hl]", raw[: raw.index(title)]):
        if marker.group() == begin:
            if first is not None:
                return None
            first = marker.start()
        else:
            # A checkpoint may start within an earlier transcript frame.
            # Once closed, it cannot enclose the selected approval frame.
            first = None
    if first is None:
        return None
    # Earlier synchronized transcript frames are not the approval rectangle.
    raw = raw[first:]
    spans, anchor, grid, covered = [], None, None, None
    while raw:
        if not raw.startswith(begin):
            return None
        finish = raw.find(end, len(begin))
        if finish < 0 or begin in raw[len(begin) : finish]:
            return None
        frame, raw = raw[len(begin) : finish], raw[finish + len(end) :]
        row = column = None
        spans = []
        tokens = re.split(rb"(\x1b\[[0-?]*[ -/]*[@-~])", frame)
        for token in tokens:
            if not token:
                continue
            if token.startswith(b"\x1b["):
                cup = re.fullmatch(rb"\x1b\[([0-9]+);([0-9]+)H", token)
                if cup:
                    row, column = map(int, cup.groups())
                    if not 1 <= row <= 40 or not 1 <= column <= 120:
                        return None
                elif re.fullmatch(rb"\x1b\[[0-9;]*m", token) or token in (
                    b"\x1b[?25h",
                    b"\x1b[?25l",
                ):
                    pass
                else:
                    return None
                continue
            if row is None or b"\x1b" in token or any(byte < 32 or byte == 127 for byte in token):
                return None
            ascii_text = all(byte < 127 for byte in token)
            if column is None:
                return None
            if ascii_text:
                if column + len(token) - 1 > 120:
                    return None
                text = token.decode("ascii")
                if "Cline needs permission" in text:
                    if anchor is not None or text.count("Cline needs permission") != 1:
                        return None
                    anchor = row
                    if anchor + 8 > 40:
                        return None
                    grid = [[" "] * 120 for _ in range(9)]
                    covered = [[False] * 120 for _ in range(9)]
                spans.append((row, column, text))
                column += len(token)
            else:
                # UTF-8 byte count bounds terminal cell width conservatively.
                # Refuse potential wrapping even when the starting row is
                # outside the panel, since it may overwrite panel rows.
                if column + len(token) - 1 > 120:
                    return None
                spans.append((row, column, None))
                # Width of unrelated native Unicode text is not guessed. An
                # explicit absolute position must precede another printable run.
                column = None
        if anchor is not None:
            for r, c, text in spans:
                if anchor <= r <= anchor + 8:
                    if text is None:
                        return None
                    for offset, char in enumerate(text):
                        grid[r - anchor][c - 1 + offset] = char
                        covered[r - anchor][c - 1 + offset] = True
    if anchor is None or not all(all(line) for line in covered):
        return None
    return ["".join(line).strip() for line in grid]


def fresh_approval_panel(term, checkpoint, command):
    """Bounded post-submit native full panel, never cumulative said() text."""
    with term._lock:
        raw = bytes(term._out[checkpoint:])
    if len(raw) > 256 * 1024:
        raise AssertionError("Native approval output exceeded its bound")
    lines = positioned_approval_rows(raw)
    if lines is None:
        return False
    text = "\n".join(lines)
    if text.count("Cline needs permission") != 1 or text.count("Approve tool call?") != 1:
        return False
    required = ["Cline needs permission", "Approve tool call?", "run_commands", "$ " + command]
    positions = []
    for wanted in required:
        matches = [index for index, line in enumerate(lines) if line == wanted]
        if len(matches) != 1:
            return False
        positions.append(matches[0])
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        return False
    tail = "\n".join(lines[positions[-1] + 1 :])
    return tail.count("[y] Approve") == tail.count("[n] Deny") == 1 and not any(
        line.startswith("$ ") for line in lines if line != "$ " + command
    )


def answer_native_approval(
    term, checkpoint, messages, command, level, responded, *, child_running, marker_exists
):
    pending = pending_command_call(messages, command)
    if (
        pending is None
        or pending["id"] in responded
        or not fresh_approval_panel(term, checkpoint, pending["input"]["commands"][0])
    ):
        return None
    assert level in ("ask-allow", "ask-reject"), "Approval outside ask grant"
    assert not child_running(), "Command ran before native approval"
    assert not marker_exists(), "Command marker existed before native approval"
    term.send(b"n" if level == "ask-reject" else b"y")
    responded[pending["id"]] = level
    return pending["id"]


def positioned_question_rows(raw):
    """Measured ten-row question panel; ASCII fields plus fixed scrollbar cell."""
    begin, end = b"\x1b[?2026h", b"\x1b[?2026l"
    title = b"Cline is asking a question"
    if raw.count(title) != 1:
        return None
    first = None
    for marker in re.finditer(rb"\x1b\[\?2026[hl]", raw[: raw.index(title)]):
        if marker.group() == begin:
            if first is not None:
                return None
            first = marker.start()
        else:
            # A checkpoint may start within an earlier transcript frame.
            # Once closed, it cannot enclose the selected approval frame.
            first = None
    if first is None:
        return None
    # Earlier synchronized transcript frames are not the approval rectangle.
    raw = raw[first:]
    spans, anchor, grid, covered = [], None, None, None
    while raw:
        if not raw.startswith(begin):
            return None
        finish = raw.find(end, len(begin))
        if finish < 0 or begin in raw[len(begin) : finish]:
            return None
        frame, raw = raw[len(begin) : finish], raw[finish + len(end) :]
        row = column = None
        spans = []
        tokens = re.split(rb"(\x1b\[[0-?]*[ -/]*[@-~])", frame)
        for token in tokens:
            if not token:
                continue
            if token.startswith(b"\x1b["):
                cup = re.fullmatch(rb"\x1b\[([0-9]+);([0-9]+)H", token)
                if cup:
                    row, column = map(int, cup.groups())
                    if not 1 <= row <= 40 or not 1 <= column <= 120:
                        return None
                elif re.fullmatch(rb"\x1b\[[0-9;]*m", token) or token in (
                    b"\x1b[?25h",
                    b"\x1b[?25l",
                ):
                    pass
                else:
                    return None
                continue
            if row is None or b"\x1b" in token or any(byte < 32 or byte == 127 for byte in token):
                return None
            ascii_text = all(byte < 127 for byte in token)
            if column is None:
                return None
            if ascii_text:
                if column + len(token) - 1 > 120:
                    return None
                text = token.decode("ascii")
                if "Cline is asking a question" in text:
                    if anchor is not None or text.count("Cline is asking a question") != 1:
                        return None
                    anchor = row
                    if anchor + 9 > 40:
                        return None
                    grid = [[" "] * 120 for _ in range(10)]
                    covered = [[False] * 120 for _ in range(10)]
                spans.append((row, column, text, row))
                column += len(token)
            elif token == b"\xe2\x96\x88" and column == 119:
                # Pinned native vertical scrollbar paints U+2588 with one
                # setCellWithAlphaBlending call (binary offset103578989).
                spans.append((row, column, " ", row))
                column += 1
            else:
                # UTF-8 bytes conservatively bound unrelated text's possible
                # wrap rows. No unknown width may touch the question panel.
                last_row = row + (column + len(token) - 2) // 120
                if last_row > 40:
                    return None
                spans.append((row, column, None, last_row))
                # Width of unrelated native Unicode text is not guessed. An
                # explicit absolute position must precede another printable run.
                column = None
        if anchor is not None:
            for r, c, text, last_row in spans:
                if text is None:
                    if r <= anchor + 9 and last_row >= anchor:
                        return None
                    continue
                if anchor <= r <= anchor + 9:
                    for offset, char in enumerate(text):
                        grid[r - anchor][c - 1 + offset] = char
                        covered[r - anchor][c - 1 + offset] = True
    if anchor is None or not all(all(line) for line in covered):
        return None
    return ["".join(line).strip() for line in grid]


CLARIFICATION_RESPONSE = "Do not run any command. Report NO-SHELL and stop."
USER_REJECTION = ("This tool call was rejected by the user and not executed. -- "
                  "NOT a tool or system failure. Clarify with user before proceeding.")


def pending_rejection_clarification(messages, command, responded):
    """Only the measured own rejected command followed by one fresh question."""
    records = native_tool_records(messages)
    if records is None:
        return None
    calls, results = records
    if len(calls) != 2 or len(results) != 1:
        return None
    (command_index, own), (question_index, question) = calls
    result_index, result = results[0]
    if (own['name'] != 'run_commands' or not command_input(own.get('input'), command)
            or responded != {own['id']: 'ask-reject'}
            or result['tool_use_id'] != own['id'] or result.get('is_error') is not True
            or not command_index < result_index < question_index
            or question_index != len(messages) - 1 or question['name'] != 'ask_question'):
        return None
    try:
        value = json.loads(result.get('content'))
    except (ValueError, TypeError):
        return None
    if value != {'error': USER_REJECTION}:
        return None
    inputs = question.get('input')
    if (not isinstance(inputs, dict) or set(inputs) != {'question', 'options'}
            or not isinstance(inputs['question'], str) or not inputs['question'].isascii()
            or not 1 <= len(inputs['question']) <= 234
            or not isinstance(inputs['options'], list) or not 2 <= len(inputs['options']) <= 5
            or not all(isinstance(option, str) and option.isascii() and 1 <= len(option) <= 114
                       for option in inputs['options'])
            or len(set(inputs['options'])) != len(inputs['options'])):
        return None
    return question


def fresh_question_panel(term, checkpoint, question):
    with term._lock:
        raw = bytes(term._out[checkpoint:])
    assert len(raw) <= 256 * 1024, 'Native question output exceeded its bound'
    rows = positioned_question_rows(raw)
    inputs = question['input']
    if rows is None or rows[:2] != ['Cline is asking a question', '']:
        return False
    # Native f7 renders the full question above its source-defined options.
    # Only complete measured ten-row rectangles authorize custom input; a
    # clipped/scrolled question or option inventory cannot borrow hidden text.
    question_end = next((i for i in range(2, len(rows)) if rows[i] == ''), None)
    if question_end not in (3, 4):
        return False
    question_rows = rows[2:question_end]
    if (any(not 1 <= len(row) <= 117 for row in question_rows)
            or ' '.join(question_rows) != inputs['question']):
        return False
    return rows == ['Cline is asking a question', '', *question_rows, '',
                    '> ' + inputs['options'][0], *inputs['options'][1:],
                    'Type a response...', '', '']


def answer_native_rejection_clarification(
    term, checkpoint, messages, command, level, responded, clarified, *, child_running, marker_exists
):
    if level != 'ask-reject' or clarified:
        return None
    question = pending_rejection_clarification(messages, command, responded)
    if question is None or not fresh_question_panel(term, checkpoint, question):
        return None
    assert not child_running(), 'Command ran after native rejection'
    assert not marker_exists(), 'Command marker existed after native rejection'
    # This is an explicit test-person answer. Native f7 custom input accepts
    # printable text then Enter; no option is selected and no command retried.
    term.send(CLARIFICATION_RESPONSE.encode('ascii') + b'\r')
    clarified[question['id']] = CLARIFICATION_RESPONSE
    return question['id']


def unavailable_command_refusal(result):
    """Pinned native parse failure, before execution; never authorizes the input."""
    if result.get("is_error") is not True or not isinstance(result.get("content"), str):
        return False
    try:
        value = json.loads(result["content"])
    except (json.JSONDecodeError, TypeError):
        return False
    if (
        not isinstance(value, dict)
        or set(value) != {"error"}
        or not isinstance(value["error"], str)
    ):
        return False
    prefix = (
        "Tool call run_commands was rejected before execution: AI_NoSuchToolError: "
        "Model tried to call unavailable tool 'run_commands'. Available tools: "
    )
    if not value["error"].startswith(prefix):
        return False
    inventory = value["error"][len(prefix) :]
    if re.fullmatch(r"[A-Za-z0-9_]+(?:, [A-Za-z0-9_]+)*\.", inventory) is None:
        return False
    return "run_commands" not in inventory[:-1].split(", ")


def terminal_command_candidate(snapshot, command, digest, level, marker, responded):
    """Exact messages+idle is only a trigger for native /quit, never a pass."""
    messages = snapshot["messages"]
    row = snapshot["row"]
    if row["status"] != "idle" or not messages:
        return False
    final = messages[-1]
    if (
        final.get("role") != "assistant"
        or final.get("metadata", {}).get("displayOnly")
        or final.get("metadata", {}).get("displayRole") == "error"
        or not any(
            p.get("type") == "text" and isinstance(p.get("text"), str) and p["text"].strip()
            for p in final.get("content", [])
        )
        or any(p.get("type") == "tool_use" for p in final.get("content", []))
    ):
        return False
    records = native_tool_records(messages)
    if records is None:
        return False
    calls, results = records
    if len(calls) != len(results) or any(index >= len(messages) - 1 for index, _ in results):
        return False
    own = [part for _, part in calls if part["name"] == "run_commands"]
    if level == "deny":
        return (
            all(
                unavailable_command_refusal(
                    next(part for _, part in results if part["tool_use_id"] == call["id"])
                )
                for call in own
            )
            and not responded
            and not marker
            and any(
                p.get("type") == "text" and p.get("text", "").strip() == "NO-SHELL"
                for p in final["content"]
            )
        )
    if len(own) != 1 or not command_input(own[0].get("input"), command):
        return False
    call = own[0]
    result = next(part for _, part in results if part["tool_use_id"] == call["id"])
    output = json.dumps(result.get("content"), ensure_ascii=False)
    if level == "ask-reject":
        return (
            responded == {call["id"]: level}
            and result.get("is_error") is True
            and digest not in output
            and not marker
        )
    if level == "ask-allow" and responded != {call["id"]: level}:
        return False
    if level == "allow" and responded:
        return False
    return (
        level in ("allow", "ask-allow")
        and result.get("is_error") is not True
        and digest in output
        and marker.strip() == digest
    )


def terminal_command_outcome(before_stop, after_stop, command, digest, level, marker, responded):
    """Persisted same-session completed/exit0/ended is the actual verdict."""
    if not terminal_command_candidate(before_stop, command, digest, level, marker, responded):
        return False
    before, after, manifest = before_stop["row"], after_stop["row"], after_stop["manifest"]
    stable = (
        "session_id",
        "provider",
        "model",
        "cwd",
        "workspace_root",
        "interactive",
        "is_subagent",
        "prompt",
        "started_at",
        "messages_path",
    )
    if (
        any(before.get(key) != after.get(key) for key in stable)
        or before_stop["messages"] != after_stop["messages"]
    ):
        return False
    if (
        after.get("status") != "completed"
        or type(after.get("exit_code")) is not int
        or after["exit_code"] != 0
        or not isinstance(after.get("ended_at"), str)
        or not after["ended_at"]
    ):
        return False
    try:
        started = datetime.fromisoformat(after["started_at"])
        ended = datetime.fromisoformat(after["ended_at"])
        if ended < started:
            return False
    except (ValueError, TypeError):
        return False
    return (
        manifest.get("session_id") == after["session_id"]
        and manifest.get("status") == "completed"
        and type(manifest.get("exit_code")) is int
        and manifest["exit_code"] == 0
        and manifest.get("ended_at") == after["ended_at"]
        and manifest.get("provider") == after["provider"]
        and manifest.get("model") == after["model"]
    )


def owned_native_path(home, path):
    """Native persisted paths must stay in this real, non-linked owned home."""
    home, path = Path(home).absolute(), Path(path).absolute()
    assert not home.is_symlink(), "Native home is a symlink"
    assert home.is_dir(), "Native home missing"
    try:
        relative = path.relative_to(home)
    except ValueError:
        raise AssertionError("Native path outside owned home") from None
    assert relative.parts and all(part not in (".", "..") for part in relative.parts)
    current = home
    for part in relative.parts:
        current = current / part
        assert not current.is_symlink(), "Native persisted path contains a symlink"
    assert current.resolve(strict=True).is_relative_to(home.resolve(strict=True))
    return current


def native_message_text(home, path):
    """Hold no-follow directory descriptors while opening the native message."""
    path = owned_native_path(home, path)
    relative = path.relative_to(Path(home).absolute())
    directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(
            relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            assert stat.S_ISREG(os.fstat(fd).st_mode), "Native messages are not a regular file"
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                return stream.read()
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(directory)


def native_sessions(home):
    """Only this owned native database; reject linked/nonregular stores first."""
    assert not home.is_symlink(), "Native home is a symlink"
    if not home.exists():
        return []
    databases = list(home.rglob("sessions.db"))
    if not databases:
        return []
    assert len(databases) == 1, "Ambiguous native session database"
    database = owned_native_path(home, databases[0])
    fd = os.open(database, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        assert stat.S_ISREG(os.fstat(fd).st_mode), "Native database is not regular"
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT session_id,provider,model,messages_path,status,"
                    "ended_at,exit_code,started_at,prompt,cwd,workspace_root,interactive,is_subagent FROM sessions"
                )
            ]
    finally:
        os.close(fd)


def native_conversation(home, harness_session_id, model, prompt):
    rows = [row for row in native_sessions(home) if row["session_id"] == harness_session_id]
    assert len(rows) == 1, "Native session identity missing or ambiguous"
    row = rows[0]
    assert (row["provider"], row["model"]) == ("openrouter", model), "Native selected model differs"
    assert row["prompt"] == '<user_input mode="act">' + prompt + "</user_input>", (
        "Native prompt differs from the submitted turn"
    )
    assert row["cwd"] == row["workspace_root"] == "/workspace"
    assert row["interactive"] == 1 and row["is_subagent"] == 0
    assert row["messages_path"], "Native messages path missing"
    relative = Path(row["messages_path"]).relative_to(cline.CLINE_HOME)
    path = home / relative
    document = json.loads(native_message_text(home, path))
    assert document.get("version") == 1 and document.get("sessionId") == harness_session_id
    assert isinstance(document.get("messages"), list)
    assert "/" not in harness_session_id and harness_session_id not in (".", "..")
    manifest_path = path.parent / (harness_session_id + ".json")
    manifest = json.loads(native_message_text(home, manifest_path))
    return {"row": row, "messages": document["messages"], "manifest": manifest}


def assert_private(prepared):
    key = os.environ["OPENROUTER_API_KEY"]
    if key in json.dumps(prepared.env) or any(name.endswith("API_KEY") for name in prepared.env):
        pytest.fail("Selected key exposed in terminal exec environment")
    directory = Path(cline.key_settings_host_dir())
    path = directory / "providers.json"
    settings = json.loads(path.read_bytes())
    if settings["providers"]["openrouter"]["settings"]["apiKey"] != key:
        pytest.fail("Native file does not contain exactly selected credential")
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    # Read the native file to obtain the witness inside the own container; the
    # scanner's argv/env never contain the actual key. Emit booleans only.
    script = """import json,pathlib
p=pathlib.Path('/home/coder/.cline/collimate-key/providers.json')
k=json.loads(p.read_text())['providers']['openrouter']['settings']['apiKey'].encode()
bad=[]
for d in pathlib.Path('/proc').iterdir():
 if d.name.isdigit():
  for leaf in ('cmdline','environ'):
   try: raw=(d/leaf).read_bytes()
   except FileNotFoundError: continue
   if k in raw: bad.append([d.name,leaf])
print(json.dumps({'private':not bad,'witnesses':bad}))"""
    # Match the native container user. Root without ptrace capability cannot
    # inspect coder's /proc environment, so switching UID breaks the scanner.
    rc, text = prepared.env_obj.exec(["python3", "-c", script], timeout=10)
    assert rc == 0, "Native process scanner failed before producing a privacy verdict"
    assert json.loads(text)["private"], "Selected key found in process argv/environment"
    rc, text = prepared.env_obj.exec(["env"], timeout=5)
    assert rc == 0 and key not in text
    if key in json.dumps(prepared.env_obj.container.attrs.get("Config", {}).get("Env", [])):
        pytest.fail("Selected key exposed in container configuration")


def command_child_running(environment, command):
    script = """import os,pathlib,sys
command=sys.argv[1].encode(); me=str(os.getpid())
# DockerEnvironment.exec(timeout=5) wraps this scanner in its own timeout.
# Prove that immediate parent before excluding its command-bearing argv.
own=pathlib.Path('/proc')/me
parent=next(line.split()[1] for line in (own/'status').read_text().splitlines() if line.startswith('PPid:'))
ancestor=pathlib.Path('/proc')/parent
args=(own/'cmdline').read_bytes().split(b'\\0')[:-1]
parent_args=(ancestor/'cmdline').read_bytes().split(b'\\0')[:-1]
assert pathlib.Path(os.readlink(ancestor/'exe')).name=='timeout', 'Unproven scanner timeout executable'
assert parent_args==[b'timeout',b'5',*args], 'Unproven scanner timeout argv'
found=False
for d in pathlib.Path('/proc').iterdir():
 if d.name.isdigit() and d.name not in (me,parent):
  try: argv=(d/'cmdline').read_bytes()
  except FileNotFoundError: continue
  if command in argv: found=True
print(found)"""
    rc, output = environment.exec(["python3", "-c", script, command], user="root", timeout=5)
    assert rc == 0 and output.strip() in {"True", "False"}
    return output.strip() == "True"


@pytest.mark.parametrize("level", ["deny", "ask-reject", "ask-allow", "allow", "private-allow"])
def test_cline_actual_terminal_commands_and_private_file(
    docker_env_bridge, monkeypatch, level, tmp_path
):
    commands = "allow" if level == "private-allow" else level.partition("-")[0]
    docker_env_bridge.permissions = {"docker": "full", "internet": "full", "commands": commands}
    docker_env_bridge.packages = dict(PINS)
    model = model_for("openrouter")
    bind_model_account(model_account("openrouter", model=model, harness="cline"))
    digest = conformance._write_probe()
    command = "sha256sum commands-probe.txt | cut -d ' ' -f1 | tee /workspace/commands-probe.ran"
    home = Path(harness_homes.harness_home("cline"))
    old_sessions = {row["session_id"] for row in native_sessions(home)}
    terms = _host_terminals(docker_env_bridge, monkeypatch)
    prepared = []
    real_prepare = cline.prepare

    async def capture(*args, **kwargs):
        result = await real_prepare(*args, **kwargs)
        prepared.append(result)
        return result

    monkeypatch.setattr(cline, "prepare", capture)

    async def run():
        task = asyncio.create_task(
            surface_runner("cline", "terminal")(
                {
                    "backend": "cline",
                    "auth": "api_key",
                    "provider": "openrouter",
                    "model": model,
                    "commands": commands,
                    "websearch": False,
                },
                pointer=gen.run_path("terminal/cline.terminal"),
            )
        )
        checkpoint, latest_snapshot, responded, clarified = None, None, {}, {}
        try:
            deadline = asyncio.get_running_loop().time() + 60
            while not terms and not task.done() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            assert terms and prepared, "Actual terminal runner did not register"
            term = next(iter(terms.values()))
            _, blocked = await asyncio.to_thread(_up_to_prompt, "cline", term)
            assert not blocked
            assert_private(prepared[-1])
            candidate = None
            if level != "private-allow":
                prompt = conformance._probe_prompt(command)
                checkpoint = terminal_checkpoint(term)
                await asyncio.to_thread(_type_line, term, prompt)
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    rows = [
                        row
                        for row in native_sessions(home)
                        if row["session_id"] not in old_sessions
                    ]
                    assert len(rows) <= 1, "Unexpected additional native session"
                    if rows and rows[0]["prompt"]:
                        harness_session_id = rows[0]["session_id"]
                        try:
                            snapshot = native_conversation(home, harness_session_id, model, prompt)
                            latest_snapshot = snapshot
                        except (FileNotFoundError, json.JSONDecodeError):
                            # Native snapshot writes may be in progress; incomplete
                            # bytes cannot authorize a key or establish a verdict.
                            await asyncio.sleep(0.1)
                            continue
                        marker_path = Path(gen.workspace_path()) / "commands-probe.ran"
                        marker = marker_path.read_text() if marker_path.exists() else ""
                        answer_native_approval(
                            term,
                            checkpoint,
                            snapshot["messages"],
                            command,
                            level,
                            responded,
                            child_running=lambda: command_child_running(
                                prepared[-1].env_obj, command
                            ),
                            marker_exists=marker_path.exists,
                        )
                        answer_native_rejection_clarification(
                            term, checkpoint, snapshot['messages'], command, level, responded, clarified,
                            child_running=lambda: command_child_running(prepared[-1].env_obj, command),
                            marker_exists=marker_path.exists,
                        )
                        if terminal_command_candidate(
                            snapshot, command, digest, level, marker, responded
                        ):
                            candidate = snapshot
                            break
                    assert not task.done(), "Native terminal ended before its completed candidate"
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("Native command candidate did not arrive within240seconds")
                assert_private(prepared[-1])
            # Actual native local UI command: await its ordinary session stop
            # and process exit before allowing the SDK runner's finally cleanup.
            await asyncio.to_thread(_type_line, term, "/quit")
            deadline = time.monotonic() + 20
            while term.proc.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert term.proc.poll() == 0, "Native /quit did not exit normally within20seconds"
            if candidate is not None:
                rows = [
                    row for row in native_sessions(home) if row["session_id"] not in old_sessions
                ]
                assert {row["session_id"] for row in rows} == {candidate["row"]["session_id"]}
                final = native_conversation(home, candidate["row"]["session_id"], model, prompt)
                latest_snapshot = final
                marker_path = Path(gen.workspace_path()) / "commands-probe.ran"
                marker = marker_path.read_text() if marker_path.exists() else ""
                assert terminal_command_outcome(
                    candidate, final, command, digest, level, marker, responded
                ), "Native same-session persisted completion/grant verdict failed"
            # The program exited, so the leg ends by itself.
            await asyncio.wait_for(task, timeout=30)
            directory = Path(cline.key_settings_host_dir())
            assert not directory.exists() or not list(directory.iterdir())
        finally:
            try:
                for index, term in enumerate(terms.values()):
                    capture_terminal_evidence(
                        tmp_path / f"cline-terminal-{index}.json",
                        term,
                        checkpoint=checkpoint,
                        level=level,
                        responded=responded,
                        clarified=clarified,
                        snapshot=latest_snapshot,
                        selected_key=os.environ["OPENROUTER_API_KEY"],
                    )
            finally:
                try:
                    for term in terms.values():
                        if term.proc.poll() is None:
                            try:
                                term.send(b"\x03\x03")
                            except AssertionError:
                                # An exit race cannot skip owned runner cleanup.
                                pass
                    # A program still up past the interrupt has its client
                    # ended: the leg ends with its terminal.
                    deadline = time.monotonic() + 10
                    while (any(term.proc.poll() is None for term in terms.values())
                           and time.monotonic() < deadline):
                        await asyncio.sleep(0.1)
                    for term in terms.values():
                        if term.proc.poll() is None:
                            await asyncio.to_thread(term.stop)
                    await asyncio.wait_for(task, timeout=30)
                finally:
                    for term in terms.values():
                        term.stop()

    run_on_test_loop(run())
