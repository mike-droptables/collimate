# SPDX-License-Identifier: MIT
"""Exact selected-secret hygiene at agent-owned diagnostic persistence boundaries.

This does not prevent an allow-command process from reading or encoding its
credentials. Registry values stay in this generator process, never in the cache.
"""
from __future__ import annotations

import json
import re
import threading

_LOCK = threading.RLock()
_VALUES: set[bytes] = set()
_JSON_VALUES: set[bytes] = set()
_JSON_PATTERN = None
_JSON_WIDTH = 1
_REVISION = 0
_PATTERN = None
_WIDTH = 1
_MARKER = b'[redacted]'
_TOKEN_KEYS = {'apikey', 'accesstoken', 'refreshtoken', 'idtoken', 'oauthtoken',
               'token', 'password', 'secret', 'access', 'refresh', 'authorization',
               'personalaccesstoken', 'cachedtoken'}


def clear():
    """Reset isolated test/runtime state; production retains values until exit."""
    global _REVISION, _PATTERN, _WIDTH, _JSON_PATTERN, _JSON_WIDTH
    with _LOCK:
        _VALUES.clear()
        _JSON_VALUES.clear()
        _JSON_PATTERN, _JSON_WIDTH = None, 1
        _REVISION += 1
        _PATTERN, _WIDTH = None, 1


def remember(value):
    global _REVISION, _PATTERN, _WIDTH, _JSON_PATTERN, _JSON_WIDTH
    if not isinstance(value, str) or not value or value == 'not-required':
        return
    # Credentials and selected native login documents are bounded by their
    # account reader. Reject pathological individual strings without logging.
    if len(value.encode('utf-8')) > 1 << 20:
        raise ValueError('Selected credential exceeds the redaction size bound')
    json_forms = {json.dumps(value)[1:-1].encode(), json.dumps(value, ensure_ascii=False)[1:-1].encode()}
    forms = {value.encode()} | json_forms
    with _LOCK:
        if forms <= _VALUES:
            return
        additions = forms - _VALUES
        if len(_VALUES) + len(additions) > 4096 or sum(map(len, _VALUES | forms)) > 8 << 20:
            raise ValueError('Selected credentials exceed the redaction registry bound')
        _VALUES.update(forms)
        _JSON_VALUES.update(json_forms)
        _JSON_WIDTH = max(map(len, _JSON_VALUES))
        _JSON_PATTERN = re.compile(b'|'.join(re.escape(v) for v in sorted(_JSON_VALUES, key=lambda x: (-len(x), x))))
        _REVISION += 1
        _WIDTH = max(map(len, _VALUES))
        _PATTERN = re.compile(b'|'.join(re.escape(v) for v in sorted(_VALUES, key=lambda x: (-len(x), x))))


def remember_document(value):
    """Register token fields in a selected login JSON document, not metadata."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub('[^a-z]', '', str(key).lower())
            if normalized in _TOKEN_KEYS and isinstance(item, str):
                remember(item)
            elif isinstance(item, (dict, list)):
                remember_document(item)
    elif isinstance(value, list):
        for item in value:
            remember_document(item)


def remember_account(account):
    secrets = (account or {}).get('secrets') or {}
    for value in secrets.values():
        if isinstance(value, str):
            remember(value)
            try:
                remember_document(json.loads(value))
            except (ValueError, TypeError):
                pass
        else:
            remember_document(value)
    return account


def snapshot(*, json_strings=False):
    with _LOCK:
        return (_REVISION, _JSON_PATTERN, _JSON_WIDTH) if json_strings else (_REVISION, _PATTERN, _WIDTH)


def scrub_bytes(value: bytes) -> bytes:
    _revision, pattern, _width = snapshot()
    return pattern.sub(_MARKER, value) if pattern else value


def scrub(value: str) -> str:
    return scrub_bytes(str(value).encode('utf-8')).decode('utf-8')


def scrub_object(value):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {scrub(k) if isinstance(k, str) else k: scrub_object(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_object(v) for v in value]
    return value


class ByteScrubber:
    """Bounded raw suffix; JSON matches must span whole encoded characters."""
    def __init__(self, *, json_strings=False, escape_state=0):
        self.pending = b''
        self.json_strings = json_strings
        # -1 means the preceding byte was a backslash; positive values count
        # the remaining hex digits of a Unicode escape at the committed cut.
        self.escape_state = escape_state

    def _consume(self, data, *, final=False):
        raw = self.pending + data
        _revision, pattern, width = snapshot(json_strings=self.json_strings)
        end = len(raw) if final or not pattern else max(0, len(raw) - width + 1)
        states = None
        if self.json_strings:
            state = self.escape_state
            states = [state]
            for char in raw:
                if state == -1:
                    state = 4 if char == 117 else 0
                elif state > 0:
                    state -= 1
                elif char == 92:
                    state = -1
                states.append(state)
        output = bytearray()
        pos = cursor = 0
        if pattern:
            while (match := pattern.search(raw, cursor)) is not None:
                if match.start() >= end:
                    break
                if states is not None and (states[match.start()] or states[match.end()]):
                    # An invalid match may overlap a later valid one.
                    cursor = match.start() + 1
                    continue
                output.extend(raw[pos:match.start()])
                output.extend(_MARKER)
                pos = cursor = match.end()
        end = max(end, pos)
        output.extend(raw[pos:end])
        self.pending = raw[end:]
        if states is not None:
            self.escape_state = states[end]
        return bytes(output)

    def feed(self, data: bytes) -> bytes:
        return self._consume(data)

    def finish(self) -> bytes:
        return self._consume(b'', final=True)


def refresh():
    # Only the explicitly leased native credential document; no ambient files.
    from .login_store import remember_current_credentials
    remember_current_credentials()


def write_log(path, content, *, category='log', **kwargs):
    import gen
    refresh()
    content = scrub_persisted(path, content)
    return gen.update_file(path, content, category=category, **kwargs)


class JsonByteScrubber:
    """Scrub only JSON string contents, preserving syntax and incremental state.

    Native JSONL files can end inside a growing string. The checkpoint is the
    lexer state at the retained raw suffix, so the next sync resumes there.
    """
    def __init__(self, checkpoint=(False, False, 0)):
        self._inside, self._escaped, escape_state = checkpoint
        self._value = ByteScrubber(json_strings=True, escape_state=escape_state)
        self._cut_escape = self._escaped

    @property
    def pending(self):
        return self._value.pending

    @property
    def checkpoint(self):
        return self._inside, self._cut_escape, self._value.escape_state

    def _text(self, data):
        raw = self._value.pending + data
        result = self._value.feed(data)
        consumed = raw[:len(raw) - len(self.pending)]
        if consumed:
            suffix = len(consumed) - len(consumed.rstrip(b'\\'))
            self._cut_escape = (self._cut_escape ^ bool(suffix % 2)
                                if suffix == len(consumed) else bool(suffix % 2))
        return result

    def feed(self, data):
        output, text = bytearray(), bytearray()
        for char in data:
            if not self._inside:
                output.append(char)
                if char == 34:
                    self._inside = True
                    self._escaped = self._cut_escape = False
            elif char == 34 and not self._escaped:
                output.extend(self._text(bytes(text)))
                text.clear()
                output.extend(self._value.finish())
                output.append(char)
                self._inside = self._cut_escape = False
            else:
                text.append(char)
                self._escaped = not self._escaped if char == 92 else False
        if text:
            output.extend(self._text(bytes(text)))
        return bytes(output)

    def finish(self):
        return self._value.finish()


def scrub_persisted(path, content):
    if snapshot()[1] is None:
        return content
    raw = content if isinstance(content, bytes) else str(content).encode()
    if (str(path).lower().endswith(('.json', '.jsonl', '.ndjson'))
            and raw.lstrip()[:1] in (b'{', b'[', b'\"')):
        stream = JsonByteScrubber()
        cleaned = stream.feed(raw) + stream.finish()
    else:
        cleaned = scrub_bytes(raw)
    return cleaned if isinstance(content, bytes) else cleaned.decode('utf-8')
