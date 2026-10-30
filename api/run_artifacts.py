"""Per-run Config/Logs artifact store.

``run_artifacts`` (channel migration v5) is the owner table for the two
non-Files artifact categories of a run: 'config' — captured host-side at
kickoff (prompt.md, the effective settings files, the staged runner config
folders, run.yaml) — and 'log' — the projected run log written at
finalization plus any generator-declared log artifacts. Bytes live in the
ordinary CAS; rows here are what makes them reachable (the GC mark set in
``cas_gc`` unions this table into its live set).

Config is host-written only: nothing arriving over IPC may claim the
'config' category (see process_manager.artifacts), so a run's config
provenance cannot be forged by generator code.

The host owns two fixed 'log' paths: ``logs.txt``, its own run log, and
``annotations.json``, the run's annotation context written when it is
sealed. Generator log artifacts are refused both paths so the host's
records can never be clobbered.
"""

import sqlite3

from collimate._host.ipc import ANNOTATIONS_LOG, RESERVED_LOG_NAMES

from cas import hash_and_store_bytes, require_blob

# The host-owned log slots. Generator log artifacts may use any other path.
RUN_LOG_FILE = "logs.txt"
RUN_ANNOTATIONS_FILE = ANNOTATIONS_LOG
HOST_LOG_FILES: frozenset[str] = frozenset(RESERVED_LOG_NAMES)


def record_artifact_bytes(
    conn: sqlite3.Connection,
    run_id: str,
    category: str,
    filepath: str,
    data: bytes,
) -> str:
    """Admit required bytes and record their artifact reference or raise.

    The caller owns the transaction and commits only after all captures succeed.
    """
    blob_hash = hash_and_store_bytes(data)
    conn.execute(
        "INSERT OR IGNORE INTO blobs (hash, size) VALUES (?, ?)",
        (blob_hash, len(data)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO run_artifacts (run_id, category, filepath, blob_hash) "
        "VALUES (?, ?, ?, ?)",
        (run_id, category, filepath, blob_hash),
    )
    return blob_hash


def record_artifact_blob(
    conn: sqlite3.Connection,
    run_id: str,
    category: str,
    filepath: str,
    blob_hash: str,
) -> None:
    """Verify a required existing body before recording its artifact reference.

    No body copy is created; the caller commits the reference with its mutation.
    """
    try:
        size = len(require_blob(blob_hash))
    except OSError as exc:
        raise OSError(f"Required artifact body unavailable for {filepath}: {exc}") from exc
    conn.execute(
        "INSERT OR IGNORE INTO blobs (hash, size) VALUES (?, ?)",
        (blob_hash, size),
    )
    conn.execute(
        "INSERT OR REPLACE INTO run_artifacts (run_id, category, filepath, blob_hash) "
        "VALUES (?, ?, ?, ?)",
        (run_id, category, filepath, blob_hash),
    )
