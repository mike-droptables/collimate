"""Private, authority-free JavaScript engine protocol; launched only by the pool."""
from __future__ import annotations

import json
import os
import resource
import struct
import sys
import tempfile

MAX_FRAME = 64 * 1024 * 1024


def read_exact(size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sys.stdin.buffer.read(size - len(chunks))
        if not chunk:
            raise EOFError
        chunks.extend(chunk)
    return bytes(chunks)


def main():
    # The parent created this empty private directory. Select it before the
    # zero-file-size limit: tempfile's default discovery writes a probe file.
    # Compiler scratch then remains under the parent's cancellation cleanup.
    tempfile.tempdir = os.getcwd()
    # Darwin's Python mappings already exceed 2 GiB (about 392 GiB measured).
    # This finite virtual-address cap is not a resident-memory guarantee;
    # the SDK separately retains its 64-MiB JS heap and 512-KiB stack limits.
    address_space = (512 if sys.platform == 'darwin' else 2) * 1024**3
    resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    from collimate._host.verification import run_verifier
    while True:
        try:
            size = struct.unpack('!I', read_exact(4))[0]
            if size > MAX_FRAME:
                return
            request = json.loads(read_exact(size))
            result = run_verifier(request['code'], request['input'], deadline=request['deadline'])
            payload = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(payload) > MAX_FRAME:
                return
            sys.stdout.buffer.write(struct.pack('!I', len(payload)) + payload)
            sys.stdout.buffer.flush()
        except EOFError:
            return


if __name__ == '__main__':
    main()
