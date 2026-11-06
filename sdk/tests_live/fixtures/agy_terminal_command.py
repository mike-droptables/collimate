# SPDX-License-Identifier: MIT
"""Measured1.2.11 native SQLite command evidence; no transport or product changes."""
import base64
from contextlib import closing
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import json
import re

COMMAND = "sha256sum commands-probe.txt | cut -d ' ' -f1 | tee /workspace/commands-probe.ran"
PROMPT = ('Use your shell tool to run exactly this command: ' + COMMAND
          + " If approval is required, wait for the human. Do not use another tool or command. "
          + "Then reply with only the command's output. If you cannot run it, reply NO-SHELL.")
MODEL = 'gemini-3.5-flash-lite'


def one(pb, blob, *path):
    for field in path:
        values = pb(blob)[field]
        assert len(values) == 1, 'Repeated scalar native field'
        blob = values[0]
    return blob


def text(pb, blob, *path):
    return one(pb, blob, *path).decode('utf-8')


def unique_json(body):
    def pairs(values):
        result = {}
        for key, value in values:
            assert key not in result, 'Duplicate native JSON key'
            result[key] = value
        return result
    return json.loads(body, object_pairs_hook=pairs)


def own_call(value, pb, *, model=MODEL, prompt=PROMPT):
    """Complete own prompt/call/arguments; SQL and embedded metadata must agree."""
    assert len(value['new_ids']) == len(value['conversations']) == 1
    conversation = value['conversations'][0]
    cid = conversation['conversation_id']
    assert value['new_ids'] == [cid] and re.fullmatch(r'[0-9a-f-]{36}', cid)
    database = conversation['database']
    assert database['table_view_partial'] is False
    tables = {t['name']: t for t in database['native_tables_uninterpreted']}
    assert len(tables) == len(database['native_tables_uninterpreted'])
    table = tables['steps']
    assert table['table_view_partial'] is False
    rows = sorted((dict(zip(table['columns'], r)) for r in table['rows']), key=lambda r: r['idx'])
    assert 3 <= len(rows) <= 4
    trajectory = tables['trajectory_meta']['rows']
    assert len(trajectory) == 1 and tuple(trajectory[0][1:]) == (cid, 4, 17)
    requests = []
    for index, row in enumerate(rows):
        assert type(row['idx']) is int and row['idx'] == index
        payload, metadata = row['step_payload'], row['metadata']
        assert type(row['step_type']) is int and one(pb, payload, 1) == row['step_type']
        assert type(row['status']) is int and one(pb, payload, 4) == row['status']
        assert one(pb, payload, 5) == metadata
        assert row['has_subtrajectory'] == row['step_format'] == 0
        assert all(row[k] is None for k in ('task_details', 'render_info'))
        for column, field in [('error_details', 31), ('permissions', 133)]:
            if row[column] is None:
                assert field not in pb(payload)
            else:
                assert index == 2 and one(pb, payload, field) == row[column]
        identity = {1: [trajectory[0][0].encode()], 4: [cid.encode()]}
        if index:
            identity[2] = [index]
        if index == 3:
            identity[3] = [1]
        assert pb(one(pb, metadata, 20)) == identity
        requests.append(text(pb, metadata, 12))
    assert len(set(requests)) == 1 and re.fullmatch(r'[0-9a-f-]{36}', requests[0])
    assert [r['step_type'] for r in rows[:3]] == [14, 15, 132]
    assert rows[0]['status'] == rows[1]['status'] == 3
    user = one(pb, rows[0]['step_payload'], 19)
    assert text(pb, user, 2) == text(pb, user, 3, 1) == prompt
    assert text(pb, user, 12, 1, 27, 8) == model
    call = one(pb, rows[2]['metadata'], 4)
    assert one(pb, rows[1]['step_payload'], 20, 7) == call
    call_id = text(pb, call, 1)
    assert re.fullmatch(r'call_[A-Za-z0-9_-]+', call_id) and text(pb, call, 2) == 'run_command'
    arguments = unique_json(text(pb, call, 3))
    assert set(arguments) == {'CommandLine', 'Cwd', 'WaitMsBeforeAsync', 'toolAction', 'toolSummary'}
    assert arguments['CommandLine'] == COMMAND and arguments['Cwd'] == '/workspace'
    assert type(arguments['WaitMsBeforeAsync']) is int and arguments['WaitMsBeforeAsync'] >= 0
    for key in ('toolAction', 'toolSummary'):
        assert isinstance(arguments[key], str) and arguments[key] and len(arguments[key]) <= 1024
        assert all(char.isprintable() for char in arguments[key])
    repeated = {}
    for entry in pb(one(pb, rows[2]['step_payload'], 140))[1]:
        assert set(pb(entry)) == {1, 2}
        key = text(pb, entry, 1)
        assert key not in repeated
        repeated[key] = text(pb, entry, 2)
    assert repeated == {k: str(v) for k, v in arguments.items()}
    return rows, {'conversation_id': cid, 'call_id': call_id, 'request_id': requests[0],
                  'trajectory_id': trajectory[0][0], 'arguments': arguments}


def permission_request(blob, pb, arguments):
    permission = one(pb, blob, 21)
    assert set(pb(permission)) == set(range(1, 8))
    assert pb(one(pb, permission, 1)) == {1: [b'command'], 2: [COMMAND.encode()]}
    assert text(pb, permission, 2) == COMMAND and one(pb, permission, 3) == 1
    reason = b'this runner asks before every command'
    assert one(pb, permission, 4) == reason
    assert text(pb, permission, 5) == arguments['toolAction']
    assert pb(one(pb, permission, 6)) == {1: [6], 2: [reason]}
    assert text(pb, permission, 7) == 'bash'


def pending_identity(value, pb):
    rows, identity = own_call(value, pb)
    assert len(rows) == 3 and rows[2]['status'] == 9
    assert rows[2]['error_details'] is rows[2]['permissions'] is None
    permission_request(one(pb, rows[2]['step_payload'], 56), pb, identity['arguments'])
    assert not value['marker']['exists'] and value['own_command_child_running'] is False
    return identity


def current_allow_panel(raw, checkpoint):
    """Full fresh panel rows; prefix decoration cannot substitute for its semantics."""
    expected = b"\x1b[?2026h\x1b[?25l\x1b[0 q\r\x1b[3A\x1b[33m\xe2\x97\x8f\x1b[m \r\n\x1b[K\n\x1b[33;1mCommand\x1b[m\x1b[K\r\n\x1b[90m\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\x1b[m\r\n\x1b[K\n\x1b[37mReason: this runner asks before every command\x1b[m\x1b[K\r\n\x1b[37;1mRequesting permission for:\x1b[m\x1b[K\r\n\x1b[37m   sha256sum commands-probe.txt | cut -d ' ' -f1 | tee /workspace/commands-probe.ran\x1b[m\x1b[K\r\n\x1b[K\n\x1b[1mRun this command?\x1b[m\x1b[K\r\n\x1b[94m> 1. Yes, run command\x1b[K\x1b[m\r\n  2. Yes, and always allow in this conversation for commands that start with 'sha256sum commands-probe.txt | cut -d ' '\x1b[K\r\n-f1 | tee /workspace/commands-probe.ra...'\x1b[K\r\n  3. Yes, and always allow for commands that start with 'sha256sum commands-probe.txt | cut -d ' ' -f1 | tee\x1b[K\r\n/workspace/commands-probe.ra...' (Persist to settings.json)\x1b[K\r\n  4. No, cancel\x1b[K\r\n\x1b[K\n  \x1b[94;1m\xe2\x86\x91/\xe2\x86\x93\x1b[90;22m Navigate \xc2\xb7 \x1b[94;1mtab\x1b[90;22m Amend \xc2\xb7 \x1b[94;1mctrl+g\x1b[90;22m edit/expand command\x1b[m\x1b[K\r\n\x1b[90mesc to cancel\x1b[m\x1b[86X\x1b[86C\x1b[2mgemini-3.5-flash-lite\r\x1b[m\x1b[?2026l"
    assert hashlib.sha256(expected).hexdigest() == '1ea9d1e58ba793291b143c7af9bbc43735184fa48eb4ed66125c90239d373768'
    start = raw.rfind(b'\x1b[?2026h')
    assert type(checkpoint) is int and 0 <= checkpoint <= start
    frame = raw[start:]
    assert frame.endswith(b'\x1b[?2026l') and len(frame) <= 8192
    def panel(value):
        assert value.count(b'\x1b[?2026h') == value.count(b'\x1b[?2026l') == 1
        value = value[len(b'\x1b[?2026h'):-len(b'\x1b[?2026l')]
        value = re.sub(rb'\x1b\[[0-9;]*m', b'', value)
        prefix, title, body = value.partition(b'Command\x1b[K\r\n')
        assert title and b'Command\x1b[K\r\n' not in body
        # Before the title native redraws may move back over a one-row tool
        # indicator or a two-row spinner. These controls precede every fresh
        # semantic panel row; none is allowed to overwrite the body afterward.
        assert re.fullmatch(rb'\x1b\[\?25l\x1b\[0 q\r\x1b\[[1-9][0-9]?A(?:\x1b\[K|[ \r\n]|\xe2\x97\x8f)*', prefix)
        body = body.replace(b'\x1b[K', b'').replace(b'\r\n', b'\n')
        footer = b'esc to cancel\x1b[86X\x1b[86C' + MODEL.encode() + b'\r'
        assert body.endswith(footer)
        rows = body[:-len(footer)]
        assert b'\x1b' not in rows and b'\r' not in rows and b'\t' not in rows
        assert len(rows.split(b'\n')) <= 30
        assert all(len(line.decode('utf8')) <= 120 for line in rows.split(b'\n'))
        return rows
    wanted = panel(expected)
    assert panel(frame) == wanted
    return {'frame_sha256': hashlib.sha256(frame).hexdigest(), 'raw_bytes': len(raw)}


def measured_rejection_transitions(raw, initial_raw):
    """Three measured incremental redraws bound to the validated initial panel.

    These frames redraw only the previous and new selection rows. The cursor
    movements are functional and measured, not a request for a full repaint.
    """
    assert raw.startswith(initial_raw)
    suffix = re.sub(rb'\x1b\[[0-9;]*m', b'', raw[len(initial_raw):])
    expected = (
        b"\x1b[?2026h\x1b[8A  1. Yes, run command\x1b[K\r\n> 2. Yes, and always allow in this conversation for commands that start with 'sha256sum commands-probe.txt | cut -d ' '\x1b[K\r\n-f1 | tee /workspace/commands-probe.ra...'\x1b[76X\x1b[?2026l",
        b"\x1b[?2026h\r\x1bM  2. Yes, and always allow in this conversation for commands that start with 'sha256sum commands-probe.txt | cut -d ' '\x1b[K\r\n-f1 | tee /workspace/commands-probe.ra...'\x1b[K\r\n> 3. Yes, and always allow for commands that start with 'sha256sum commands-probe.txt | cut -d ' ' -f1 | tee\x1b[K\r\n/workspace/commands-probe.ra...' (Persist to settings.json)\x1b[59X\x1b[?2026l",
        b"\x1b[?2026h\r\x1bM  3. Yes, and always allow for commands that start with 'sha256sum commands-probe.txt | cut -d ' ' -f1 | tee\x1b[K\r\n/workspace/commands-probe.ra...' (Persist to settings.json)\x1b[K\r\n> 4. No, cancel\x1b[K\x1b[?2026l",
    )
    assert suffix == b''.join(expected)
    return {'frame_sha256': hashlib.sha256(suffix).hexdigest(), 'raw_bytes': len(raw)}


def completed_success(value, pb, *, digest, level, responded):
    """Full native result plus the measured final assistant, before any cleanup."""
    assert level in ('ask-allow', 'allow') and re.fullmatch(r'[0-9a-f]{64}', digest)
    rows, identity = own_call(value, pb)
    assert len(rows) == 4 and rows[2]['status'] == 3
    assert rows[2]['error_details'] is None
    call = identity['call_id']
    if level == 'ask-allow':
        assert set(responded) == {call} and responded[call] == 'ask-allow'
        permissions = one(pb, rows[2]['step_payload'], 147)
        permission_request(one(pb, permissions, 1), pb, identity['arguments'])
        answer = one(pb, permissions, 2)
        assert text(pb, answer, 1) == identity['trajectory_id'] and one(pb, answer, 2) == 2
        assert pb(one(pb, answer, 21)) == {1: [1], 4: [b'']}
    else:
        assert not responded
        assert 56 not in pb(rows[2]['step_payload']) and 147 not in pb(rows[2]['step_payload']), (
            'Direct allow unexpectedly carries a native permission exchange')
    result = one(pb, rows[2]['step_payload'], 140, 2)
    assert set(pb(result)) == {1, 6, 7}
    packed = one(pb, result, 6)
    assert set(pb(packed)) == {1, 2}
    assert text(pb, packed, 1) == 'type.googleapis.com/gemini_coder.Step'
    native = one(pb, packed, 2)
    assert one(pb, native, 1) == 21 and one(pb, native, 4) == 3
    native_meta = one(pb, native, 5)
    assert one(pb, native_meta, 4) == one(pb, rows[2]['metadata'], 4)
    assert one(pb, native_meta, 20) == one(pb, rows[2]['metadata'], 20)
    assert text(pb, native_meta, 12) == identity['request_id']
    executed = one(pb, native, 28)
    assert set(pb(executed)) == {2, 6, 11, 12, 21, 23, 25, 31}
    assert text(pb, executed, 2) == '/workspace'
    assert one(pb, executed, 6) == 0 and one(pb, executed, 11) == 1
    assert one(pb, executed, 12) == identity['arguments']['WaitMsBeforeAsync']
    assert text(pb, executed, 23) == text(pb, executed, 25) == COMMAND
    assert text(pb, executed, 31) == 'bash'
    assert pb(one(pb, executed, 21)) == {1: [(digest + '\r\n').encode()]}
    assert text(pb, result, 1).replace('\r\n', '\n') == '\nThe command exited with code 0.\nOutput:\n' + digest + '\n\n'
    assert text(pb, result, 7) == ('file:///home/coder/.gemini/antigravity-cli/brain/'
                                  + identity['conversation_id'] + '/.system_generated/steps/2/output.txt')
    final = rows[3]
    assert final['step_type'] == 15 and final['status'] == 3
    assistant = one(pb, final['step_payload'], 20)
    assert 7 not in pb(assistant), 'Final assistant starts another tool'
    assert text(pb, assistant, 1) == text(pb, assistant, 8) == digest
    assert one(pb, assistant, 12) == 2
    assert 8 in pb(final['metadata']) and 8 in pb(rows[2]['metadata'])
    marker = value['marker']
    assert marker['exists'] is True and marker['truncated'] is False
    assert marker['retained_bytes'] == (digest + '\n').encode()
    assert value['own_command_child_running'] is False
    return identity


def completed_denial(value, pb, *, responded):
    """Measured native pre-tool-hook refusal, final reply and no command effects."""
    rows, identity = own_call(value, pb)
    assert len(rows) == 4 and not responded and rows[2]['status'] == 7
    assert rows[2]['permissions'] is None
    reason = b"tool call denied by pre-tool hook: this runner's commands permission is deny"
    assert pb(rows[2]['error_details']) == {2: [reason], 3: [reason]}
    payload = rows[2]['step_payload']
    assert set(pb(payload)) == {1, 4, 5, 31, 140}
    assert set(pb(one(pb, payload, 140))) == {1}, 'Denied command has a result'
    final = rows[3]
    assert final['step_type'] == 15 and final['status'] == 3
    assistant = one(pb, final['step_payload'], 20)
    assert 7 not in pb(assistant)
    assert text(pb, assistant, 1) == text(pb, assistant, 8) == 'NO-SHELL'
    assert one(pb, assistant, 12) == 2
    assert 8 in pb(final['metadata']) and 8 in pb(rows[2]['metadata'])
    assert value['marker'] == {'exists': False} and value['own_command_child_running'] is False
    return identity


def completed_rejection(value, pb, *, responded):
    """Explicit native interruption: rejected tool, no result or assistant row."""
    rows, identity = own_call(value, pb)
    assert len(rows) == 3 and rows[2]['status'] == 7
    assert responded == {identity['call_id']: 'ask-reject'}
    assert rows[2]['permissions'] is None
    payload = rows[2]['step_payload']
    assert set(pb(payload)) == {1, 4, 5, 31, 147, 140}
    assert set(pb(one(pb, payload, 140))) == {1}, 'Rejected command has a result'
    reason = ('permission check failed for command "' + COMMAND
        + '": user denied permission to run command:\n' + COMMAND
        + '\nDo not attempt to circumvent this denial by rephrasing the command, using alternative '
          'tools/scripts (e.g. python, sh, curl), or accessing the same target resource. '
          'Proceed without performing this action.').encode()
    assert pb(rows[2]['error_details']) == {2: [reason], 3: [reason]}
    permission = one(pb, payload, 147)
    permission_request(one(pb, permission, 1), pb, identity['arguments'])
    answer = one(pb, permission, 2)
    assert text(pb, answer, 1) == identity['trajectory_id'] and one(pb, answer, 2) == 2
    assert pb(one(pb, answer, 21)) == {4: [b'']}
    assert 8 in pb(rows[2]['metadata'])
    assert value['marker'] == {'exists': False} and value['own_command_child_running'] is False
    return identity


# Reused unchanged from the reviewed bounded acquisition collector.
MAX_DB = 4 * 1024 * 1024
MAX_JSON = 16 * 1024 * 1024


class Evidence:
    def __init__(self, directory, selected_key):
        if not isinstance(selected_key, str) or len(selected_key) < 8:
            raise ValueError("Selected credential unavailable")
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.forms = sorted(
            {selected_key.encode(), json.dumps(selected_key)[1:-1].encode()}, key=len, reverse=True
        )
        self.faults = []
        self.written_bytes = 0
        self._lock = threading.Lock()

    def fault(self, kind):
        with self._lock:
            if kind not in self.faults:
                self.faults.append(kind)

    def scrub(self, raw):
        for key in self.forms:
            if key in raw:
                self.fault("selected-credential-in-evidence")
                raw = raw.replace(key, b"[redacted]")
        return raw

    def blob(self, raw):
        safe = self.scrub(raw)
        return {
            "original_bytes": len(raw),
            "original_sha256": hashlib.sha256(raw).hexdigest(),
            "scrubbed": safe != raw,
            "base64": base64.b64encode(safe).decode("ascii"),
        }

    def encode(self, value):
        if isinstance(value, bytes):
            return {"native_bytes": self.blob(value)}
        if isinstance(value, str):
            return self.scrub(value.encode()).decode()
        if isinstance(value, dict):
            return {self.encode(k): self.encode(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.encode(v) for v in value]
        if isinstance(value, float) and not math.isfinite(value):
            return {"native_nonfinite_float": value.hex()}
        return value

    def write(self, name, value):
        if not re.fullmatch(r"[a-z0-9-]+\.json", name):
            raise ValueError("Evidence name rejected")
        safe = self.encode(value)
        body = json.dumps(
            {"value": safe, "capture_faults": list(self.faults)},
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        ).encode()
        if len(body) > MAX_JSON:
            self.fault("json-bound")
            raise ValueError("Evidence exceeds bound")
        if self.written_bytes + len(body) > 64 * 1024 * 1024 and name != "status.json":
            self.fault("total-evidence-bound")
            raise ValueError("Total evidence exceeds bound")
        assert all(key not in body for key in self.forms)
        fd = os.open(
            self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        self.written_bytes += len(body)


def owned_database(home, path):
    home, path = Path(home).absolute(), Path(path).absolute()
    relative = path.relative_to(home)
    if home.is_symlink() or not relative.parts or path.suffix != ".db":
        raise ValueError("Native database path refused")
    current = home
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Native database link refused")
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_DB:
        raise ValueError("Native database type/size refused")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and (
            sidecar.is_symlink()
            or not stat.S_ISREG(sidecar.stat().st_mode)
            or sidecar.stat().st_size > MAX_DB
        ):
            raise ValueError("Native database sidecar refused")
    # Do not silently ignore a WAL or create missing read coordination state.
    if Path(str(path) + "-wal").exists() and not Path(str(path) + "-shm").exists():
        raise ValueError("Native WAL lacks existing shared-memory file")
    return path


def database_snapshot(home, path):
    """Read-only SQLite backup includes committed WAL; no immutable fallback."""
    path = owned_database(home, path)
    deadline = time.monotonic() + 3
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.5)) as source:
        source.execute("PRAGMA query_only=ON")
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        if source.execute("PRAGMA page_count").fetchone()[0] * page_size > MAX_DB:
            raise ValueError("Native database logical size refused")
        with closing(sqlite3.connect(":memory:")) as target:

            def progress(_status, _remaining, total):
                if total * page_size > MAX_DB or time.monotonic() > deadline:
                    raise ValueError("Native database backup bound")

            source.backup(target, pages=64, progress=progress, sleep=0.01)
            raw = target.serialize()
            if len(raw) > MAX_DB:
                raise ValueError("Native database snapshot bound")
            schema = target.execute(
                "SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
            tables = []
            table_view_partial = False
            for kind, name, _table, _rootpage, _sql in schema:
                if kind != "table":
                    continue
                quoted = '"' + name.replace('"', '""') + '"'
                cursor = target.execute("SELECT * FROM " + quoted)
                rows = cursor.fetchmany(4097)
                partial = len(rows) > 4096
                table_view_partial |= partial
                tables.append(
                    {
                        "name": name,
                        "columns": [c[0] for c in cursor.description],
                        "rows": rows[:4096],
                        "table_view_partial": partial,
                    }
                )
    return {
        "relative_path": str(path.relative_to(Path(home).absolute())),
        "coherent_sqlite_backup": raw,
        "schema_columns": ["type", "name", "tbl_name", "rootpage", "sql"],
        "schema": schema,
        "native_tables_uninterpreted": tables,
        "table_view_partial": table_view_partial,
    }
