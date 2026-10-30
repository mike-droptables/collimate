"""The run's own-cell write path, behind the IPC ``update_cell`` op.

``_emit_partial`` is the one path a run's cell is written through: it
enforces the run folders (a run writes ordinary work files anywhere and,
under ``_runs/``, only the UI file types its manifest declares in its own
``_runs/<label>/`` — never another run's folder, a dead folder or its own
run card), accepts a name only for a cell this run created, runs the version-chain write
(``CellsMixin._update_cell``, which broadcasts the committed paths). It is
synchronous so ``_op_update_cell`` can run it under ``asyncio.to_thread`` —
the DB work and the state broadcast are the slow part of every commit and
must not stall the IPC reader's event loop.

``push_log`` is the host's own line into the run log (cleanup notices
and the like); a generator's ``gen.log`` rides ``[INFO]``-framed stderr
instead (see ``log_format``).
"""
# pyright: reportAttributeAccessIssue=false
# (mixin: attributes like log_buffer/workspace_dir live on the composed
# ProcessManager in manager.py, not on the mixin class itself)


class OwnCellWritesMixin:
    def _run_path_refusal(self, path: str, *, ui_write: bool = False) -> dict | None:
        """Why this run may not write or delete ``path`` on its cell, as an
        ack ``refused`` entry, or None when it may. The run's own folder is
        its to change (except the host-owned run card), and a cell write
        there (``ui_write``) must be a UI file type its manifest declares
        (``ui_files``); everything else under ``_runs/`` — another live
        run's folder or a dead one — is not."""
        from run_folders import in_folder, is_run_path, label_of, normalize, run_card_path
        norm = normalize(path)
        if norm == run_card_path(self.label):
            return {"path": path, "reason": "host_owned", "owner": self.label}
        if not is_run_path(norm):
            return None
        if not in_folder(norm, self.label):
            return {"path": path, "reason": "locked", "owner": label_of(norm) or ""}
        if ui_write and not any(norm.endswith(suffix) for suffix in self.ui_files):
            return {"path": path, "reason": "ui_type", "owner": self.label}
        return None

    def _emit_partial(
        self,
        *,
        artifacts: list[dict] | None = None,
        cell_name: str | None = None,
        deleted_files: list[str] | None = None,
        operation_id: str | None = None,
        writer_id: str | None = None,
        edit_seq: int | None = None,
        base_revisions: dict | None = None,
        resolves: list | None = None,
    ) -> tuple[str, list[dict]]:
        """Write ``artifacts`` and return the version ID and refused entries
        (``[{path, reason, owner}]``).

        ``artifacts`` is the list of artifact dicts (``type``,
        ``cell_path``, ``local_path`` or ``content``/``encoding``) the
        SDK's ``HostBridge.update_cell`` built from its tracked writes;
        an empty list changes no file. Entries the run folders refuse are
        dropped before the merge; handler-invalid files are refused against
        the post-flush candidate, with their error text. Accepted siblings land.
        ``cell_name`` is the name
        the update gave. It is ignored for an existing cell; None always
        leaves the cell's name as it is."""
        refused: list[dict] = []
        accepted: list[dict] = []
        for art in artifacts or []:
            # A log artifact never enters the cell, so only a cell write is
            # held to the declared UI file types.
            why = self._run_path_refusal(
                str(art.get("cell_path", "")), ui_write=art.get("category") != "log")
            if why is None:
                accepted.append(art)
            else:
                refused.append(why)
        deletions: list[str] = []
        for path in deleted_files or []:
            why = self._run_path_refusal(str(path))
            if why is None:
                deletions.append(path)
            else:
                refused.append(why)
        if not self.created_cell:
            cell_name = None

        # Only the name this emit carries, never a stand-in: an unnamed
        # update must not rename the cell. _update_cell labels the version
        # and applies the name rule.
        data: dict = {"artifacts":accepted,"name":cell_name,"operation_id":operation_id,
                      "writer_id":writer_id,"edit_seq":edit_seq,"resolves":resolves or [],
                      "base_revisions":dict(base_revisions or {})}
        if deletions:
            data["deleted_files"] = deletions

        version_id = self._update_cell(data)
        refused.extend(data.get('handler_refused', ()))
        # A UI file the run wrote over its own starting placeholder is live now:
        # an artifact surface (a webview tab) registers no session.
        handler_refused = {r['path'] for r in data.get('handler_refused', ())}
        written = [art['cell_path'] for art in accepted
                   if art.get('category') != 'log' and art['cell_path'] not in handler_refused]
        if written:
            import served_surfaces
            served_surfaces.mark_written(self.run_id, self.cell_id, written)
        for entry in refused:
            detail = entry.get('error') or entry.get('owner', '')
            self.log_refusal('update_cell', f"{entry['path']}: {entry['reason']} ({detail})")
        if cell_name is not None and data.get("metadata_receipt", {}).get("status") in {"applied", "unchanged"}:
            self._last_emit_cell_name = cell_name

        return version_id, refused

    def push_log(self, line: str) -> None:
        """Append a host-side line to the run log as a dedicated ``log``
        row, rendered as ``LOG`` alongside the generator's own
        ``[INFO]``-framed stderr."""
        self.log_buffer.append({"type": "log", "payload": line})


__all__ = ["OwnCellWritesMixin"]
