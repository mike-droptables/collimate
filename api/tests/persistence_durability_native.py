"""Owned-child native SQLite observation; no production durability override."""
from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import sqlite3


def native_probe():
    library = ctypes.CDLL(None)
    arm = library.p5_arm
    arm.argtypes = [ctypes.c_char_p] * 6 + [ctypes.c_int, ctypes.c_int]
    arm.restype = None
    library.p5_disarm.argtypes = []
    library.p5_disarm.restype = None
    return library


def observe(directory: Path, *, directory_error=False):
    """Probe installed VFS before app checks, including last-close recreation."""
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / 'native.db').resolve()
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=FULL')
    conn.execute('CREATE TABLE probe(value INTEGER PRIMARY KEY)')
    conn.commit()
    conn.close()
    native = native_probe()
    cycles = []
    for sequence in range(3):
        before_wal = Path(str(path) + '-wal').exists()
        conn = sqlite3.connect(path)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
        effective = {key: conn.execute(f'PRAGMA {key}').fetchone()[0]
                     for key in ('synchronous', 'journal_mode', 'busy_timeout')}
        conn.execute('INSERT INTO probe VALUES (?)', (sequence,))
        native.p5_arm(f'native-{sequence}'.encode(), b'installed-vfs', str(path).encode(),
                      b'isolated-native-probe', b'native_probe', b'', 0, int(directory_error))
        try:
            conn.commit()  # Raw installed SQLite; app confirmation is tested separately.
        finally:
            native.p5_disarm()
        values = [row[0] for row in conn.execute('SELECT value FROM probe ORDER BY value')]
        conn.close()
        cycles.append({'sequence': sequence, 'wal_before_open': before_wal,
                       'wal_after_last_close': Path(str(path) + '-wal').exists(),
                       'effective_pragmas': effective, 'accepted_rows': values})
    return {'sqlite_version': sqlite3.sqlite_version,
            'sqlite_source_id': sqlite3.connect(':memory:').execute('SELECT sqlite_source_id()').fetchone()[0],
            'sqlite_compile_options': [row[0] for row in sqlite3.connect(':memory:').execute('PRAGMA compile_options')],
            'injected_vfs_directory_error': directory_error, 'cycles': cycles,
            'limitations': ['Native enter/return markers bracket the syscall; no inside-disk-sync kill is claimed.',
                            'This probes installed SQLite behavior, not app durable ACK or physical power loss.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--directory-error', action='store_true')
    arguments = parser.parse_args()
    print(json.dumps(observe(arguments.directory, directory_error=arguments.directory_error), indent=2))
