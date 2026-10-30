"""Artifact hashing + file-state helpers shared by the cell write path."""
# pyright: reportAttributeAccessIssue=false
# (mixin: attributes like log_buffer/workspace_dir live on the composed
# ProcessManager in manager.py, not on the mixin class itself)

import os

from cas import MAX_BLOB_BYTES, hash_and_store_file, hash_and_store_bytes
from reserved_paths import is_reserved_path


class ArtifactsMixin:
    def _hash_artifacts(self, data: dict) -> dict[str, str]:
        """Hash artifact list from IPC payload into {cell_path: blob_hash}.

        Used by update_cell.

        Artifacts carrying ``"category": "log"`` are routed into
        ``run_artifacts(run_id, 'log', path)`` — the run node's Logs —
        instead of the returned cell-files map; they are upserted on every
        commit so a per-turn trace rewrite keeps its crash resilience.
        ``"config"`` arriving over IPC is dropped (config provenance is
        host-written only — never forgeable by generator code), as is a
        log artifact claiming one of the host's reserved slots (``logs.txt``,
        ``annotations.json``). Any other/absent category means ordinary cell
        content.
        """
        artifacts = data.get("artifacts", [])
        files: dict[str, str] = {}
        log_artifacts: list[tuple[str, str]] = []  # (path, abs local path)
        # Resolved once per emit, and only when a workspace artifact needs it
        # (a memory-only emit may come from a host with no workspace_dir).
        base = (
            os.path.realpath(self.workspace_dir)
            if any(a.get("type", "workspace") == "workspace" for a in artifacts)
            else ""
        )

        for art in artifacts:
            art_type = art.get("type", "workspace")
            cell_path = art.get("cell_path", "")
            category = art.get("category")

            if is_reserved_path(cell_path):
                continue

            if category == "config":
                self.log_buffer.append({
                    "type": "stderr",
                    "payload": (
                        f"[update_cell] dropped artifact {cell_path!r}: "
                        "'config' is host-written only"
                    ),
                })
                continue

            if category == "log":
                from run_artifacts import HOST_LOG_FILES

                if cell_path in HOST_LOG_FILES:
                    self.log_buffer.append({
                        "type": "stderr",
                        "payload": (
                            f"[update_cell] dropped log artifact {cell_path!r}: "
                            "reserved for the host's run record"
                        ),
                    })
                    continue
                if art_type == "workspace":
                    abs_path = self._contained_file(
                        base, art.get("local_path", cell_path))
                    if abs_path is None:
                        continue
                    log_artifacts.append((cell_path, abs_path))
                continue

            if art_type == "workspace":
                abs_path = self._contained_file(
                    base, art.get("local_path", cell_path))
                if abs_path is None:
                    continue
                blob_hash = self._hash_and_store_file(abs_path)
                if blob_hash:
                    files[cell_path] = blob_hash
                else:
                    try:
                        size = os.path.getsize(abs_path)
                    except OSError:
                        size = -1
                    raise OSError(f"CAS refused run file {cell_path}: {size} bytes")

            elif art_type == "memory":
                content = art.get("content", "")
                encoding = art.get("encoding", "utf-8")
                if encoding == "base64":
                    import base64
                    raw = base64.b64decode(content)
                else:
                    raw = content.encode("utf-8")
                blob_hash = self._hash_and_store_bytes(raw)
                if blob_hash:
                    files[cell_path] = blob_hash
                else:
                    raise OSError(f"CAS refused run file {cell_path}: {len(raw)} bytes")

        if log_artifacts:
            self._store_log_artifacts(log_artifacts)

        return files

    @staticmethod
    def _contained_file(base: str, local_path: str) -> str | None:
        """Resolve ``local_path`` under ``base`` (an already-realpath'd
        workspace dir) to a regular file, or None.

        The generator controls local_path over IPC; resolve it and refuse
        anything that escapes the workspace so a hostile payload can't
        pull host files into the CAS.
        """
        abs_path = os.path.realpath(os.path.join(base, local_path))
        if abs_path != base and not abs_path.startswith(base + os.sep):
            return None
        if not os.path.isfile(abs_path):
            return None
        return abs_path

    def _log_cas_refusal(self, cell_path: str, size: int) -> None:
        """Run-log row for an artifact the CAS refused to store."""
        reason = (
            f"{size} bytes exceeds the 50 MB blob cap"
            if size > MAX_BLOB_BYTES
            else "the content store could not save it"
        )
        self.log_buffer.append({
            "type": "stderr",
            "payload": f"[update_cell] dropped artifact {cell_path!r}: {reason}",
        })

    def _store_log_artifacts(self, entries: list[tuple[str, str]]) -> None:
        """Upsert generator-declared log artifacts into run_artifacts."""
        from database import get_db_connection
        from run_artifacts import record_artifact_bytes

        conn = get_db_connection(self.channel)
        try:
            for cell_path, abs_path in entries:
                try:
                    with open(abs_path, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                record_artifact_bytes(conn, self.run_id, "log", cell_path, raw)
            conn.commit()
        finally:
            conn.close()

    def _hash_and_store_file(self, abs_path: str) -> str | None:
        """Hash a file and store it in the CAS. Returns the blob hash."""
        return hash_and_store_file(abs_path)

    def _hash_and_store_bytes(self, raw: bytes) -> str | None:
        """Hash raw bytes and store them in the CAS. Returns the blob hash."""
        return hash_and_store_bytes(raw)
