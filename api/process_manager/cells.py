"""Conditional run mutations, history and accepted-file workspace refresh.

SQL revisions own current bytes. Run writes preserve competing candidates,
commit before publishing memory and use the run's workspace path projection.
"""
# pyright: reportAttributeAccessIssue=false
# (mixin: attributes like log_buffer/workspace_dir live on the composed
# ProcessManager in manager.py, not on the mixin class itself)


import hashlib
import os

from database import get_db_connection
from versions import tip_file_map, version_file_hashes


class CellsMixin:
    # ------------------------------------------------------------------
    # The run's own cell
    # ------------------------------------------------------------------

    def _update_cell(self, data: dict) -> str:
        """Write the run's cell and return its new version-chain entry ID.

        Expected revisions condition every write through the shared mutation
        service. Handler verification runs outside cell and state locks;
        the accepted snapshot is rechecked before committing eligible siblings.
        The commit event carries the write's own file touches
        (``versions.file_changes`` against the accepted files it replaced).
        Refusals return through data['handler_refused']. Once the run is
        sealed (its end, before the folder
        sweep) every emit is refused.

        ``name`` is the name the update gave, or None. It labels the
        version (an unnamed one takes the run's last name, else its
        generator id). The host has already ignored names for existing
        cells, so an accepted name belongs to a cell this run created.
        An unnamed update never renames the cell."""
        from cell_write_scope import cell_write_scope
        from persistence import MutationResult
        from versions import file_changes, insert_version
        from run_verification import RunVerifier, snapshot

        target_id = self.cell_id
        if not target_id:
            raise RuntimeError("no target cell id set")

        operation_id = data.get("operation_id")
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 256:
            raise ValueError("operation_id must be a string of 1 to 256 characters")

        deleted = {p for p in (data.get("deleted_files") or []) if isinstance(p, str)}
        import json
        # The SDK freezes workspace artifacts per operation. Its immutable
        # request identifies that snapshot; exact replay must not read or stage
        # the artifacts again after their bytes or workspace have been collected.
        operation_digest = hashlib.sha256(json.dumps(dict(artifacts=data.get("artifacts",[]),deleted=sorted(deleted),
            operation_id=data.get("operation_id"),writer_id=data.get("writer_id"),edit_seq=data.get("edit_seq"),
            base_revisions=data.get("base_revisions"),resolves=data.get("resolves",[]),name=data.get("name")),
            sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
        name = data.get("name")
        metadata_actor = None
        conn = get_db_connection(self.channel)
        try:
            if name is not None:
                from cell_lineage import runners_of, runner_actor
                metadata_actor = runner_actor(runners_of(conn, [self.run_id]).get(self.run_id))
            replay = conn.execute("SELECT cell_id,request_digest,result_json FROM file_operations WHERE operation_id=?",
                                  (data.get("operation_id"),)).fetchone()
        finally:
            conn.close()
        if replay is not None:
            if replay["cell_id"] != target_id or replay["request_digest"] != operation_digest:
                raise ValueError("Operation id was already used with different input")
            self._last_file_receipt = json.loads(replay["result_json"])
            data["handler_refused"] = self._last_file_receipt.get("refused", [])
            if name is not None:
                assert metadata_actor is not None
                from cell_metadata import finish_cell_metadata
                data["metadata_receipt"] = finish_cell_metadata(
                    self.channel, target_id, data["operation_id"], operation_digest, name,
                    source_type="generator", actor=metadata_actor)
            return self._last_file_receipt.get("cell_id", "")
        files = self._hash_artifacts(data)
        if name is not None:
            assert metadata_actor is not None
            from cell_metadata import prepare_cell_metadata
            prepare_cell_metadata(self.channel, target_id, data["operation_id"], operation_digest,
                                  name, source_type="generator", actor=metadata_actor)
        label = name if name is not None else (self._last_emit_cell_name or self.generator_name)

        receipt: MutationResult
        version_id = ''
        with RunVerifier(self) as verifier:
            for attempt in range(2):
                verifier.prepare()
                with cell_write_scope(self.channel,[target_id]):
                    conn = get_db_connection(self.channel)
                    try:
                        prepared = snapshot(self, conn, files, deleted)
                    finally:
                        conn.close()
                refused = verifier.check(prepared)
                # No cell, state or SQL transaction lock spans a verifier call.
                with cell_write_scope(self.channel,[target_id]):
                    conn = get_db_connection(self.channel)
                    try:
                        current = snapshot(self, conn, files, deleted)
                        if (current.identity != prepared.identity
                                or verifier.current_key() != verifier.catalog_key):
                            if attempt == 0:
                                continue
                            refused = verifier.raced(current)
                        refused.update(current.policy_refused)
                        tip_id, tip_files = current.tip, current.tip_files
                        merged = current.candidate(refused)
                        from persistence import mutate_files,snapshot_files
                        from cas import read_blob
                        mutations=[]
                        for path in sorted(current.writes.keys() | deleted):
                            if path in refused:
                                continue
                            if path not in data.get('base_revisions',{}):
                                raise ValueError('Missing expected file revision: ' + path)
                            mutation=dict(path=path,writer_id=data.get('writer_id'),edit_seq=data.get('edit_seq'),
                                          base_revision=data['base_revisions'][path],kind='delete' if path in deleted else 'put')
                            if path not in deleted:
                                digest=current.writes[path]
                                artifact = next((a for a in data.get("artifacts",[]) if a.get("cell_path")==path),{})
                                raw=read_blob(digest)
                                if raw is None:
                                    raise OSError('Missing run artifact ' + path)
                                if artifact.get("encoding") in ("binary","base64"):
                                    mutation.update(blob_hash=digest,encoding="binary")
                                else:
                                    try:
                                        mutation["content_text"]=raw.decode("utf-8")
                                    except UnicodeDecodeError:
                                        if artifact.get("encoding") == "utf-8":
                                            raise ValueError("Artifact is not UTF-8: " + path)
                                        mutation.update(blob_hash=digest,encoding="binary")
                            mutations.append(mutation)
                        prior_receipt = conn.execute('SELECT result_json FROM file_operations WHERE operation_id=?',
                                                     (data.get('operation_id'),)).fetchone()
                        if mutations:
                            receipt=mutate_files(self.channel,target_id,dict(operation_id=data.get('operation_id'),
                                mutations=mutations,resolves=data.get('resolves',[])),actor='run',manager=self,conn=conn,request_digest=operation_digest)
                        else:
                            receipt={'operation_id': operation_id, 'outcome': 'accepted', 'files': [], 'conflicts': []}
                        self._last_file_receipt=receipt
                        version_id=''
                        event=None
                        if mutations and receipt['outcome']=='accepted' and prior_receipt is None:
                            merged={f['path']:f['body_hash'] for f in snapshot_files(conn,target_id)}
                            changes=file_changes(current.base,merged,[m['path'] for m in mutations])
                            self._ensure_genesis_row(conn,tip_id)
                            gen_version,_tip=self._provenance_for_write(conn)
                            version_id,event=insert_version(conn,tip_id=tip_id,parent_files=tip_files,
                                run_id=self.run_id,files=merged,file_entries=snapshot_files(conn,target_id),
                                version_name=label,source_type='generator',
                                generator_name=self.generator_name,generator_cell_id=gen_version,owner_id=target_id,
                                changes=changes)
                            self._stamp_input_edges(conn,version_id)
                        if prior_receipt is not None:
                            version_id = receipt.get('cell_id', '')
                        else:
                            receipt['cell_id'] = version_id
                            receipt['refused'] = list(refused.values())
                            if mutations:
                                conn.execute('UPDATE file_operations SET result_json=? WHERE operation_id=?',
                                             (json.dumps(receipt, sort_keys=True), data['operation_id']))
                            else:
                                conn.execute('INSERT INTO file_operations(operation_id,cell_id,request_digest,result_json) '
                                             'VALUES (?,?,?,?)', (data['operation_id'], target_id,
                                                                  operation_digest, json.dumps(receipt, sort_keys=True)))
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    finally:
                        conn.close()
                    if name is not None:
                        assert metadata_actor is not None
                        from cell_metadata import finish_cell_metadata
                        data["metadata_receipt"] = finish_cell_metadata(
                            self.channel, target_id, data["operation_id"], operation_digest, name,
                            source_type="generator", actor=metadata_actor)
                    data['handler_refused'] = list(refused.values())
                    break
        from persistence import _publish_safely
        _publish_safely(self.channel,target_id,receipt)
        if event is not None:
            self._broadcast_run_events([event])
            self._broadcast_dag_commit(target_id,[f['path'] for f in receipt['files']])
        return version_id

    def _ensure_genesis_row(self, conn, cell_id: str) -> None:
        """A stub ``genesis`` row for a tip with no cells row, so the version
        edge has a parent. The run-start flush normally made it real."""
        if conn.execute("SELECT 1 FROM cells WHERE id=? LIMIT 1", (cell_id,)).fetchone():
            return
        gen_version, _tip = self._provenance_for_write(conn)
        conn.execute(
            "INSERT INTO cells (id, run_id, generator_name, generator_cell_id, "
            "sequence_num, version_name, source_type) "
            "VALUES (?, ?, ?, ?, 0, '', 'genesis')",
            (cell_id, self.run_id, self.generator_name, gen_version),
        )

    def _provenance_for_write(self, conn) -> tuple[str | None, str | None]:
        """``(generator_cell_id, input_tip_id)`` validated against real cells
        rows, memoized per run. Foreign keys are ENFORCED on the channel DB,
        and provenance must never break an emit — an id without a backing
        row (a row deleted mid-run) is dropped. Run-start flushing
        (freeze_cell_versions) guarantees real rows on the normal path."""
        cached = getattr(self, "_provenance_validated", None)
        if cached is not None:
            return cached

        def _exists(cid: str) -> bool:
            return conn.execute(
                "SELECT 1 FROM cells WHERE id=?", (cid,),
            ).fetchone() is not None

        gv = getattr(self, "generator_cell_version_id", None)
        if gv and not _exists(gv):
            gv = None
        tip = getattr(self, "input_tip_id", None)
        if tip and not _exists(tip):
            tip = None
        self._provenance_validated = (gv, tip)
        return self._provenance_validated

    def _stamp_input_edges(self, conn, cell_id: str) -> None:
        """Provenance edge ``input_0`` → ``cell_id``, pointing at the exact
        input version tip this run hydrated from (pinned at run start).

        Inert to version/tip resolution: every chain walker filters
        ``parent_key='previous_version'``. INSERT OR IGNORE keeps repeat
        emits idempotent (UNIQUE(child, parent_key)); when an input tip is
        ALSO the cell's previous_version parent, the (parent, child) primary
        key makes the input edge a no-op — the lineage is already captured
        by the version edge."""
        _gv, tip = self._provenance_for_write(conn)
        if tip and tip != cell_id:
            conn.execute(
                "INSERT OR IGNORE INTO cell_edges "
                "(parent_cell_id, child_cell_id, parent_key) VALUES (?, ?, ?)",
                (tip, cell_id, "input_0"),
            )


    def _flush_chat_files_to_workspace(self,include_tip=True):
        workspace=getattr(self,'workspace_dir',None)
        if not workspace or not self.cell_id:
            return
        conn=get_db_connection(self.channel,create=False)
        try:
            from persistence import snapshot_files
            files=snapshot_files(conn,self.cell_id,include_deleted=True)
            base=self._workspace_cell_base(conn)
        finally:
            conn.close()
        if not include_tip:
            return
        for file in files:
            if file['deleted']:
                previous = base.get(file['path'])
                if previous is None:
                    continue
                from collimate import fs
                try:
                    fd = fs.open_file(workspace, file['path'], write=False)
                except FileNotFoundError:
                    base.pop(file['path'], None)
                    continue
                except OSError as exc:
                    self._log_flush_refusal(file['path'], exc)
                    continue
                with os.fdopen(fd, 'rb') as current_file:
                    local_hash = hashlib.file_digest(current_file, 'sha256').hexdigest()
                if local_hash != previous:
                    continue
                if not self._remove_from_workspace(workspace, file['path']):
                    continue
                base.pop(file['path'], None)
                continue
            if base.get(file['path'])!=file['body_hash']:
                from collimate import fs
                try:
                    fd = fs.open_file(workspace, file['path'], write=False)
                except FileNotFoundError:
                    fd = None
                except OSError as exc:
                    self._log_flush_refusal(file['path'], exc)
                    continue
                if fd is not None:
                    with os.fdopen(fd, 'rb') as current_file:
                        local_hash = hashlib.file_digest(current_file, 'sha256').hexdigest()
                    if local_hash != base.get(file['path']):
                        continue  # SDK owns unresolved local candidates.
                elif file['path'] in base:
                    continue  # A local deletion is also unsaved work.
                if self._place_in_workspace(workspace,file['path'],blob=file['body_hash']):
                    base[file['path']]=file['body_hash']

    def _workspace_cell_base(self, conn) -> dict:
        """``{path: blob_hash}``: the cell's files as the workspace is known to
        hold them — the tip the run hydrated from, what the chat flush placed
        since, and what the run itself committed (noted by
        :meth:`_note_own_commit`, resolved here against the version that
        commit wrote). The flush's tip layer writes only the paths whose tip
        differs from it. Built on the run's first flush; the caller holds the
        cell's commit lock."""
        base = getattr(self, "_chat_cell_base", None)
        if base is None:
            hydrated = getattr(self, "input_tip_id", None)
            base = tip_file_map(conn, hydrated) if hydrated else {}
            self._chat_cell_base = base
        noted = getattr(self, "_own_commit_paths", None)
        if noted:
            by_version: dict[str, list[str]] = {}
            for path, version in noted.items():
                by_version.setdefault(version, []).append(path)
            noted.clear()
            for version, paths in by_version.items():
                for i in range(0, len(paths), 500):
                    chunk = paths[i:i + 500]
                    committed = version_file_hashes(conn, version, chunk)
                    for path in chunk:
                        if committed.get(path):
                            base[path] = committed[path]
                        else:
                            base.pop(path, None)
        return base

    def _note_own_commit(self, data: dict, refused: list, version: str) -> None:
        """Note the cell paths an ``update_cell`` just committed from this
        run's workspace to ``version``, so the
        chat flush counts them as content the workspace already has."""
        refused_paths = {entry.get("path") for entry in refused}
        paths = [art.get("cell_path") for art in data.get("artifacts") or []
                 if art.get("category") != "log"]
        paths += list(data.get("deleted_files") or [])
        noted = getattr(self, "_own_commit_paths", None)
        if noted is None:
            noted = self._own_commit_paths = {}
        for path in paths:
            if isinstance(path, str) and path not in refused_paths:
                noted[path] = version

    def _place_in_workspace(self, workspace: str, path: str, *,
                            blob: str | None = None, data: bytes = b"") -> bool:
        """Write the cell file ``path`` into the workspace — the CAS ``blob``'s
        bytes, or ``data`` — through ``collimate.fs``, which follows no
        symlinked component. False, with nothing written, when the blob is
        missing from the CAS or the path is refused (logged)."""
        import shutil
        from cas import blob_path
        from collimate import fs
        try:
            if "\\" in path:
                raise PermissionError("a backslash is not a cell path separator")
            import hashlib
            expected_size = os.path.getsize(blob_path(blob)) if blob is not None else len(data)
            expected_hash = blob if blob is not None else hashlib.sha256(data).hexdigest()
            try:
                fd = fs.open_file(workspace, path, write=False)
            except FileNotFoundError:
                fd = None
            if fd is not None:
                with os.fdopen(fd, "rb") as existing:
                    if os.fstat(existing.fileno()).st_size == expected_size:
                        digest = hashlib.sha256()
                        while chunk := existing.read(65536):
                            digest.update(chunk)
                        if digest.hexdigest() == expected_hash:
                            return True
            if blob is None:
                fs.write_file(workspace, path, data)
                return True
            try:
                src = open(blob_path(blob), "rb")
            except FileNotFoundError:
                return False
            with src, os.fdopen(fs.open_file(workspace, path, write=True), "wb") as dst:
                shutil.copyfileobj(src, dst)
            return True
        except OSError as exc:
            self._log_flush_refusal(path, exc)
            return False

    def _remove_from_workspace(self, workspace: str, path: str) -> bool:
        """Remove the workspace entry ``path`` (a symlink itself, never its
        target) through ``collimate.fs``, without following a symlinked
        parent. False when the path is refused (logged)."""
        from collimate import fs
        try:
            if "\\" in path:
                raise PermissionError("a backslash is not a cell path separator")
            fs.unlink(workspace, path)
            return True
        except OSError as exc:
            self._log_flush_refusal(path, exc)
            return False

    def _log_flush_refusal(self, path: str, exc: OSError) -> None:
        self.log_buffer.append({
            "type": "stderr",
            "payload": f"[chat flush] refused {path}: {exc}",
        })

    def _cell_live_metadata(self,paths=None,revisions=None,include_deleted=False):
        """The run cell's accepted descriptors by path (the cell path is the
        workspace path)."""
        from persistence import snapshot_files,descriptor
        from reserved_paths import is_reserved_path
        conn=get_db_connection(self.channel,create=False)
        try:
            if revisions is None:
                rows=snapshot_files(conn,self.cell_id,include_deleted=include_deleted)
            else:
                rows=[]
                for path,revision in revisions.items():
                    row=conn.execute('SELECT * FROM file_revisions WHERE cell_id=? AND path=? AND revision=?',
                                     (self.cell_id,path,revision)).fetchone()
                    if row is None or row['deleted']:
                        raise FileNotFoundError(path)
                    rows.append(descriptor(row))
            return {f['path']:f for f in rows
                    if not is_reserved_path(f['path']) and (paths is None or f['path'] in paths)}
        finally:
            conn.close()

    def _compute_cell_live_files(self,paths=None):
        from cas import read_blob
        files={}
        for path,entry in self._cell_live_metadata(paths).items():
            raw=read_blob(entry['body_hash'])
            if raw is None:
                raise OSError('Missing accepted file '+path)
            files[path]=raw
        return files

    def _compute_cell_live_hashes(self,paths=None):
        return {p:{'body_hash':f['body_hash'],'file_revision':f['file_revision'],'encoding':f['encoding'],'deleted':f['deleted']}
                for p,f in self._cell_live_metadata(paths,include_deleted=True).items()}
