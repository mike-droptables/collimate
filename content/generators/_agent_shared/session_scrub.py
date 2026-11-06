# SPDX-License-Identifier: MIT
"""Sanitize session-cache copies, retaining incremental JSONL source offsets."""
from __future__ import annotations

import contextlib
import os
import stat
import tempfile

from collimate import fs
from .secret_hygiene import ByteScrubber, JsonByteScrubber, snapshot

_TEXT = {}
_SCANS = {}
_BLOCK = 65536


def _stamp(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _source(st):
    return (*_stamp(st), st.st_ctime_ns)


def opaque(fd, path):
    if not path.lower().endswith(('.json', '.jsonl', '.ndjson', '.txt', '.md', '.log', '.yaml', '.yml', '.toml')):
        return True
    head = os.pread(fd, 512, 0)
    if b'\x00' in head:
        return True
    try:
        head.decode('utf-8')
    except UnicodeDecodeError as exc:
        # A multibyte character truncated at the sample edge is still text.
        return exc.end != len(head)
    return False


def contaminated(fd):
    revision, pattern, width = snapshot()
    if pattern is None:
        return False
    st = os.fstat(fd)
    key = (_source(st), revision)
    if key in _SCANS:
        return _SCANS[key]
    offset, carry, found = 0, b'', False
    while offset < st.st_size:
        part = os.pread(fd, min(_BLOCK, st.st_size - offset), offset)
        if not part:
            break
        offset += len(part)
        data = carry + part
        if pattern.search(data):
            found = True
            break
        carry = data[-(width - 1):] if width > 1 else b''
    if len(_SCANS) > 4096:
        _SCANS.clear()
    _SCANS[key] = found
    return found


def stage_text(fd, st, dst):
    """(handled, staged_path). Unchanged or safely appended text returns None."""
    revision, pattern, _width = snapshot()
    if pattern is None or opaque(fd, dst):
        return False, None
    try:
        have = os.stat(dst, follow_symlinks=False)
    except OSError:
        have = None
    if have is not None and stat.S_ISDIR(have.st_mode):
        raise IsADirectoryError(dst)
    old = _TEXT.get(dst)
    valid = (old and have is not None and stat.S_ISREG(have.st_mode)
             and old['target'] == _stamp(have) and old['revision'] == revision)
    if valid and old['source'] == _source(st):
        return True, None
    append = (valid and dst.endswith('.jsonl') and old['source'][:2] == _source(st)[:2]
              and st.st_size > old['source'][2]
              and os.pread(fd, len(old['tail']), old['tail_at']) == old['tail'])
    # A previously empty/whitespace prefix can become JSON on its first
    # append. Restart once so it cannot inherit the plain-text scrubber.
    if (append and old['json_state'] is None
            and dst.lower().endswith(('.json', '.jsonl', '.ndjson'))
            and os.pread(fd, 512, 0).lstrip()[:1] in (b'{', b'[', b'\"')):
        append = False
    tmp = None
    outfd = -1
    rollback = None
    try:
        if append:
            outfd = os.open(dst, os.O_RDWR | fs.O_NOFOLLOW)
            if _stamp(os.fstat(outfd)) != old['target']:
                os.close(outfd)
                outfd = -1
                append = False
        if append:
            offset, outpos = old['source_cut'], old['output_cut']
            rollback = os.pread(outfd, have.st_size - outpos, outpos)
            os.lseek(outfd, outpos, os.SEEK_SET)
        else:
            outfd, tmp = tempfile.mkstemp(prefix='.secret-', suffix='.collimate-tmp', dir=os.path.dirname(dst))
            offset, outpos = 0, 0
        json_text = (old['json_state'] is not None if append else
                     dst.lower().endswith(('.json', '.jsonl', '.ndjson'))
                     and os.pread(fd, 512, 0).lstrip()[:1] in (b'{', b'[', b'\"'))
        scrubber = (JsonByteScrubber(old['json_state'] if append else (False, False, 0))
                    if json_text else ByteScrubber())
        with os.fdopen(os.dup(outfd), 'wb') as out:
            while offset < st.st_size:
                block = os.pread(fd, min(_BLOCK, st.st_size - offset), offset)
                if not block:
                    raise OSError('Session source changed during scrub')
                offset += len(block)
                data = scrubber.feed(block)
                out.write(data)
                outpos += len(data)
            source_cut = offset - len(scrubber.pending)
            output_cut = outpos
            json_state = scrubber.checkpoint if json_text else None
            out.write(scrubber.finish())
            out.flush()
            os.ftruncate(outfd, out.tell())
            if _source(os.fstat(fd)) != _source(st):
                raise OSError('Session source changed during scrub')
            os.fchmod(outfd, stat.S_IMODE(st.st_mode) & 0o777)
            os.utime(outfd, ns=(st.st_atime_ns, st.st_mtime_ns))
            target = _stamp(os.fstat(outfd))
        tail_at = max(0, st.st_size - _BLOCK)
        record = dict(source=_source(st), target=target, revision=revision,
                      source_cut=source_cut, output_cut=output_cut, json_state=json_state,
                      tail_at=tail_at, tail=os.pread(fd, st.st_size - tail_at, tail_at))
        if len(_TEXT) >= 256:
            _TEXT.clear()
        _TEXT[dst] = record
        return True, tmp
    except BaseException:
        if append and outfd >= 0 and rollback is not None:
            os.lseek(outfd, old['output_cut'], os.SEEK_SET)
            os.write(outfd, rollback)
            os.ftruncate(outfd, have.st_size)
            os.utime(outfd, ns=(have.st_atime_ns, have.st_mtime_ns))
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise
    finally:
        if outfd >= 0:
            os.close(outfd)
