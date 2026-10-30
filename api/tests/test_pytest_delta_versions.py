"""Immutable logical snapshots with bounded physical metadata writes (P4F03)."""
import uuid


def _channel_id(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'https://collimate.test/channels/' + label))


def _body_hash(body):
    from cas import store_bytes
    return store_bytes(body.encode('utf-8'))


def test_small_emit_writes_changed_metadata_only__P4F03(fresh_cwd, monkeypatch):
    import state
    import versions
    from database import get_db_connection
    from models import AppState, Cell, Channel, Stack, VirtualFile
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED

    files = [VirtualFile(name=f'f{i}.txt', content=f'original {i}') for i in range(20)]
    cell = Cell(files=files)
    channel = Channel(name='delta-scale', main_stacks=[Stack(cells=[cell])])
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(channel)
    monkeypatch.setattr(state, 'state', AppState(channels=[channel]))
    run = str(uuid.uuid4())
    conn = get_db_connection(str(channel.id))
    conn.execute("INSERT INTO runs(id,status) VALUES (?,'running')", (run,))
    versions.synthesize_genesis_row(conn, str(cell.id), run, 0,
        files)
    from persistence import initialize_files
    initialize_files(conn,str(cell.id),files)
    conn.commit()
    conn.close()
    manager = ProcessManager(channel=str(channel.id), run_id=run, cell_id=str(cell.id),
        app_dir=str(fresh_cwd), workspace_dir=str(fresh_cwd), entrypoint='unused.py',
        generator_name='scale', runner_id='scale', runner_name='Scale', permissions=UNRESTRICTED)
    version, refused = manager._emit_partial(artifacts=[
        {'type': 'memory', 'cell_path': 'changed.txt', 'content': 'a' * 1024}],operation_id=str(uuid.uuid4()),writer_id=run,edit_seq=1,base_revisions={'changed.txt':0})
    assert not refused
    conn = get_db_connection(str(channel.id))
    try:
        assert len(versions.tip_file_map(conn, version)) == 21
        assert conn.execute('SELECT COUNT(*) FROM cell_files WHERE cell_id=?', (version,)).fetchone()[0] == 1
    finally:
        conn.close()


def _root(conn, files=None):
    import versions
    run, root = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute("INSERT INTO runs(id,status) VALUES (?,'running')", (run,))
    versions.synthesize_genesis_row(conn, root, run, 0,
        [__import__('models').VirtualFile(name=p,content=text) for p,text in (files or {}).items()])
    return run, root


def _append(conn, run, tip, files, owner):
    import versions
    previous = {f['path']: f for f in versions.version_file_entries(conn, tip)}
    return versions.insert_version(conn, tip_id=tip,
        parent_files=versions.tip_file_map(conn, tip), files=files,
        file_entries=[dict(path=p,body_hash=h,encoding=previous.get(p,{}).get('encoding','binary' if p=='binary' else 'utf-8'),language=previous.get(p,{}).get('language','text')) for p,h in files.items()],
        owner_id=owner, run_id=run, version_name='Edit', source_type='manual_edit',
        generator_name='test')[0]


def test_delta_edit_delete_readd_empty_and_rollback__P4F03(fresh_cwd):
    from cas import store_bytes
    from database import get_db_connection
    from versions import tip_file_map, find_tip
    conn = get_db_connection(_channel_id('delta'))
    run, root = _root(conn, {'kept': 'unchanged', 'edited': 'old'})
    original = tip_file_map(conn, root)
    changed = {**original, 'edited': store_bytes(b'new'), 'binary': store_bytes(b'\xff\x00')}
    v1 = _append(conn, run, root, changed, root)
    deleted = {'binary': changed['binary']}
    v2 = _append(conn, run, v1, deleted, root)
    v3 = _append(conn, run, v2, {}, root)
    v4 = _append(conn, run, v3, changed, root)
    conn.commit()
    assert [tip_file_map(conn, v) for v in (root, v1, v2, v3, v4)] == [original, changed, deleted, {}, changed]
    assert conn.execute('SELECT blob_hash FROM cell_files WHERE cell_id=? AND filepath=?', (v2, 'edited')).fetchone()[0] is None
    failed = _append(conn, run, v4, {'failed': store_bytes(b'failed')}, root)
    assert find_tip(conn, root) == failed
    conn.rollback()
    assert find_tip(conn, root) == v4
    assert tip_file_map(conn, v4) == changed
    assert not tip_file_map(conn, failed)
    conn.close()
    conn = get_db_connection(_channel_id('delta'))
    assert find_tip(conn, root) == v4 and tip_file_map(conn, v4) == changed
    conn.close()


def test_tip_accelerator_obeys_direct_edge_branch_delete_rewrite__P4F03(fresh_cwd):
    from database import get_db_connection
    from versions import find_tip
    conn = get_db_connection(_channel_id('graph'))
    run, root = _root(conn)
    left = _append(conn, run, root, {}, root)
    left_end = _append(conn, run, left, {}, root)
    _, right = _root(conn)
    conn.execute("INSERT INTO cell_edges VALUES (?,?,'previous_version')", (root, right))
    assert find_tip(conn, root) == right
    assert find_tip(conn, left) == left_end
    conn.execute("DELETE FROM cell_edges WHERE parent_cell_id=? AND child_cell_id=?", (root, right))
    assert find_tip(conn, root) == left_end
    conn.execute("UPDATE cell_edges SET child_cell_id=? WHERE parent_cell_id=? AND child_cell_id=?", (right, left, left_end))
    assert find_tip(conn, root) == right
    conn.rollback()
    conn.close()


def test_long_chain_latest_map_and_tip_do_not_replay_after_reopen__P4F03(fresh_cwd):
    from cas import store_bytes
    from database import get_db_connection
    from versions import find_tip, tip_file_map
    conn = get_db_connection(_channel_id('long'))
    run, root = _root(conn, {'retained': 'base'})
    expected = tip_file_map(conn, root)
    tip = root
    history = []
    for i in range(500):
        expected = {**expected, 'changing': store_bytes(str(i).encode())}
        tip = _append(conn, run, tip, expected, root)
        history.append(tip)
    conn.commit()
    conn.close()
    conn = get_db_connection(_channel_id('long'))
    instructions = []
    conn.set_progress_handler(lambda: instructions.append(1) or 0, 100)
    assert find_tip(conn, root) == tip
    assert tip_file_map(conn, tip) == expected
    conn.set_progress_handler(None, 0)
    assert len(instructions) < 10  # SQLite VM work, not just query count.
    assert tip_file_map(conn, history[4])['changing'] == store_bytes(b'4')
    assert conn.execute('SELECT COUNT(*) FROM cell_files').fetchone()[0] == 501
    conn.close()


def test_gc_keeps_every_versions_files_live_inherited_included__P4F03_O02(fresh_cwd):
    """Every version row is live (a removed cell keeps its history): the
    root's own files, the hashes the tip inherits and the tip's new ones."""
    from cas import store_bytes
    from cas_gc import _live_blobs_in_channel
    from database import get_db_connection
    from versions import tip_file_map
    conn = get_db_connection(_channel_id('gc-delta'))
    run, root = _root(conn, {'inherited': 'keep', 'removed': 'old'})
    files = tip_file_map(conn, root)
    tip = _append(conn, run, root, {'inherited': files['inherited'], 'new': store_bytes(b'new')}, root)
    conn.commit()
    live = _live_blobs_in_channel(conn)
    assert files['inherited'] in live
    assert files['removed'] in live
    assert set(tip_file_map(conn, tip).values()) <= live
    conn.close()


def test_v13_migration_preserves_complete_empty_and_branch_snapshots__P4F03(tmp_path):
    import sqlite3
    import migrations
    import versions
    conn = sqlite3.connect(tmp_path / 'old.db')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    migrations.apply(conn, [m for m in migrations.CHANNEL_MIGRATIONS if m[0] <= 13])
    conn.execute("INSERT INTO runs(id) VALUES ('run')")
    for i, name in enumerate(('root', 'left', 'left-tip', 'right', 'empty')):
        conn.execute("INSERT INTO cells(id,run_id,sequence_num) VALUES (?,'run',?)", (name, i))
    for parent, child in (('root','left'), ('left','left-tip'), ('root','right')):
        conn.execute("INSERT INTO cell_edges VALUES (?,?,'previous_version')", (parent, child))
    for version, path, digest in (('root','a',_body_hash('a')), ('left','b',_body_hash('b')),
                                   ('left-tip','c',_body_hash('c')), ('right','d',_body_hash('d'))):
        conn.execute('INSERT INTO blobs(hash,size) VALUES (?,1)', (digest,))
        conn.execute('INSERT INTO cell_files VALUES (?,?,?)', (version, path, digest))
    conn.commit()
    assert migrations.apply(conn, [m for m in migrations.CHANNEL_MIGRATIONS if m[0] <= 14]) == [14]
    assert versions.tip_file_map(conn, 'root') == {'a':_body_hash('a')}
    assert versions.tip_file_map(conn, 'left-tip') == {'c':_body_hash('c')}
    assert versions.tip_file_map(conn, 'empty') == {}
    assert versions.find_tip(conn, 'root') == 'right'
    assert versions.find_tip(conn, 'left') == 'left-tip'
    # Old historical snapshots are not all duplicated into the mutable index.
    assert conn.execute('SELECT COUNT(*) FROM cell_file_current').fetchone()[0] == 2
    assert not migrations.apply(conn, [m for m in migrations.CHANNEL_MIGRATIONS if m[0] <= 14])
    conn.close()


def test_emit_row_bounds_at_20_200_2000_files__P4F03(fresh_cwd, monkeypatch):
    """Actual manager emits, durable flush, and trigger-counted SQL row writes."""
    import collections
    import hashlib
    import json
    import sqlite3
    import time
    import cas
    import cell_commits
    import state
    import versions
    from database import get_db_connection
    from models import AppState, Cell, Channel, Stack, VirtualFile
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED

    measurements = []
    for count in (20, 200, 2000):
        files = []
        for i in range(count):
            size = (48 if i < count * 19 // 20 else 112) * 1024
            prefix = f'{i:08d}:'
            files.append(VirtualFile(name=f'f{i:04d}.txt', content=prefix + 'x' * (size-len(prefix))))
        total_bytes = sum(len(f.content) for f in files)
        if count == 2000:
            assert total_bytes == 104_857_600
        cell = Cell(files=files)
        channel = Channel(name=f'scale-{count}', main_stacks=[Stack(cells=[cell])])
        from genesis import ensure_capabilities_stack
        ensure_capabilities_stack(channel)
        monkeypatch.setattr(state, 'state', AppState(channels=[channel]))
        run = str(uuid.uuid4())
        conn = get_db_connection(str(channel.id))
        conn.execute("INSERT INTO runs(id,status) VALUES (?,'running')", (run,))
        versions.synthesize_genesis_row(conn, str(cell.id), run, 0,
            files)
        from persistence import initialize_files, publish_files
        initialize_files(conn,str(cell.id),files)
        conn.commit()
        # Hydration populates immutable body identities before measuring edits.
        publish_files(channel,str(cell.id),{},persist=False)
        state.persist_state()
        conn.execute('CREATE TABLE measured_writes(table_name TEXT, operation TEXT, row_bytes INTEGER)')
        tables = ('cells', 'cell_edges', 'cell_files', 'cell_file_versions', 'cell_file_heads',
                  'cell_file_current', 'cell_version_heads', 'blobs', 'run_events', 'file_revisions', 'working_files', 'file_operations', 'file_conflicts', 'file_path_sequences', 'file_writer_sequences')
        for table in tables:
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                owner = 'OLD' if operation == 'DELETE' else 'NEW'
                size = '+'.join(f"COALESCE(length(CAST({owner}.{c} AS BLOB)),0)" for c in columns)
                conn.execute(f"CREATE TRIGGER measure_{table}_{operation} AFTER {operation} ON {table} BEGIN "
                             f"INSERT INTO measured_writes VALUES ('{table}','{operation}',{size}); END")
        conn.commit()
        manager = ProcessManager(channel=str(channel.id), run_id=run, cell_id=str(cell.id),
            app_dir=str(fresh_cwd), workspace_dir=str(fresh_cwd), entrypoint='unused.py',
            generator_name='scale', runner_id='scale', runner_name='Scale', permissions=UNRESTRICTED)
        for series, path in (('new-path', 'delta.txt'), ('existing-path', 'f0000.txt')):
            conn.execute('DELETE FROM measured_writes')
            conn.commit()
            hashed, read = [], []
            hashed_by_source = collections.Counter()
            parameters = collections.Counter()
            original_hash, original_new, original_read = hashlib.sha256, hashlib.new, cas.read_blob
            original_connect = sqlite3.connect
            class MeasuredConnection(sqlite3.Connection):
                def measure(self, sql, values):
                    if sql.lstrip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                        parameters[self.measurement_kind] += sum(len(v.encode()) if isinstance(v, str)
                            else len(v) if isinstance(v, bytes) else 8 if v is not None else 0 for v in values)

                def execute(self, sql, parameters=()):
                    self.measure(sql, parameters)
                    return super().execute(sql, parameters)

                def executemany(self, sql, parameters):
                    rows = list(parameters)
                    for values in rows:
                        self.measure(sql, values)
                    return super().executemany(sql, rows)

            def connect(path, *args, **kwargs):
                kwargs['factory'] = MeasuredConnection
                connection = original_connect(path, *args, **kwargs)
                connection.measurement_kind = 'layout' if str(path).endswith('app.db') else 'channel'
                return connection

            def read_blob(digest):
                raw = original_read(digest)
                read.append(len(raw or b''))
                return raw
            class HashProbe:
                def __init__(self, inner, data=b''):
                    import inspect
                    frame = inspect.currentframe().f_back
                    while frame and frame.f_code.co_filename in {__file__, hashlib.__file__}:
                        frame = frame.f_back
                    self.source = frame.f_code.co_filename.rsplit('/', 1)[-1] if frame else 'unknown'
                    hashed.append(len(data))
                    hashed_by_source[self.source] += len(data)
                    self.inner = inner
                def update(self, data):
                    hashed.append(len(data))
                    hashed_by_source[self.source] += len(data)
                    self.inner.update(data)
                def copy(self):
                    copied = object.__new__(HashProbe)
                    copied.inner = self.inner.copy()
                    copied.source = self.source
                    return copied
                def __getattr__(self, name):
                    return getattr(self.inner, name)

            def sha256(data=b'', **kwargs):
                return HashProbe(original_hash(data, **kwargs), data)

            def new(name, data=b'', **kwargs):
                inner = original_new(name, data, **kwargs)
                return HashProbe(inner, data) if inner.name == 'sha256' else inner

            with monkeypatch.context() as probe:
                probe.setattr(sqlite3, 'connect', connect)
                probe.setattr(hashlib, 'sha256', sha256)
                probe.setattr(hashlib, 'new', new)
                # Both direct hashing and file_digest must be observable. The
                # latter constructs through hashlib.new, not hashlib.sha256.
                import io
                control = hashlib.sha256(b'a')
                control.update(b'bc')
                assert control.hexdigest() == original_hash(b'abc').hexdigest()
                assert sum(hashed) == 3
                hashed.clear()
                streamed = hashlib.file_digest(io.BytesIO(b'abc'), 'sha256')
                assert streamed.hexdigest() == control.hexdigest()
                assert sum(hashed) == 3
                hashed.clear()
                hashed_by_source.clear()
                for module, name in ((cas, 'read_blob'), (cell_commits, 'blob_bytes')):
                    probe.setattr(module, name, read_blob)
                start = time.perf_counter()
                for i in range(3):
                    body = str(i) + ('a' if series == 'new-path' else 'b') * 1023
                    tip, refused = manager._emit_partial(artifacts=[{'type':'memory', 'cell_path':path, 'content':body}],operation_id=str(uuid.uuid4()),writer_id=run,edit_seq=i+1,base_revisions={path:i+(1 if series=='existing-path' else 0)})
                    assert not refused
                state.flush_persist_state()
                elapsed = time.perf_counter()-start
            rows = list(conn.execute('SELECT table_name,operation,row_bytes FROM measured_writes'))
            counts = collections.Counter(row[0] for row in rows)
            assert counts['cell_files'] == 3
            assert counts['cell_file_current'] == 3
            # Each accepted edit adds immutable revision and receipt rows.
            assert counts['file_revisions'] == 3
            assert collections.Counter(row[1] for row in rows if row[0] == 'file_operations') == {
                'INSERT': 3, 'UPDATE': 3}
            assert counts['working_files'] == 3
            assert counts['file_path_sequences'] == 3
            assert counts['file_writer_sequences'] == 3
            assert len(rows) <= 48
            assert sum(row[2] for row in rows) < 12_000
            assert parameters['channel'] < 12_000
            # Eight changed-body CAS hash passes per edit, including the three
            # integrity verifications the former sha256-only probe missed when
            # they used file_digest. No unchanged body may enter this budget.
            assert hashed_by_source['cas.py'] <= 3 * 8 * 1024, dict(hashed_by_source)
            # Materialized read_blob bytes; streaming verification I/O is
            # counted by the hash probe above, not this read boundary.
            assert sum(read) <= 3 * 2 * 1024, (sum(hashed), sum(read))
            assert len(versions.tip_file_map(conn, tip)) == count + 1
            measurements.append({'files':count, 'bytes':total_bytes, 'series':series,
                'rows':dict(counts), 'total_rows':len(rows), 'row_value_bytes':sum(r[2] for r in rows),
                'hash_bytes':sum(hashed), 'hash_bytes_by_source':dict(hashed_by_source), 'materialized_cas_read_bytes':sum(read), 'seconds':round(elapsed,6)})
            measurements[-1]['write_parameter_bytes'] = dict(parameters)
        conn.close()
    # Scaling is a row bound, not a machine-dependent timing threshold.
    assert len({m['total_rows'] for m in measurements}) == 1
    print('P4F03_METADATA_MEASUREMENTS=' + json.dumps(measurements, sort_keys=True))


def test_immutable_head_read_is_atomic_with_concurrent_append__P4F03(fresh_cwd):
    from cas import store_bytes
    from database import get_db_connection
    from versions import tip_file_map
    reader, writer = get_db_connection(_channel_id('race')), get_db_connection(_channel_id('race'))
    run, root = _root(writer, {'a':'old', 'b':'retained'})
    writer.commit()
    old = tip_file_map(reader, root)
    changed = {**old, 'a': store_bytes(b'new')}
    class RacingReader:
        raced = False
        def execute(self, sql, parameters=()):
            cursor = reader.execute(sql, parameters)
            if 'cell_file_heads' in sql and not self.raced:
                self.raced = True
                _append(writer, run, root, changed, root)
                writer.commit()
            return cursor
    assert tip_file_map(RacingReader(), root) == old
    reader.close()
    writer.close()


def test_noop_delta_and_historical_fork_keep_independent_heads__P4F03(fresh_cwd):
    from cas import store_bytes
    from database import get_db_connection
    from versions import tip_file_map, find_tip
    conn = get_db_connection(_channel_id('branches'))
    run, root = _root(conn, {'a':'root', 'keep':'retained'})
    initial = tip_file_map(conn, root)
    left_files = {**initial, 'a':store_bytes(b'left')}
    left = _append(conn, run, root, left_files, root)
    noop = _append(conn, run, left, left_files, root)
    assert conn.execute('SELECT COUNT(*) FROM cell_files WHERE cell_id=?', (noop,)).fetchone()[0] == 0
    right_files = {**initial, 'right':store_bytes(b'right')}
    right = _append(conn, run, root, right_files, root)
    conn.commit()
    assert tip_file_map(conn, left) == tip_file_map(conn, noop) == left_files
    assert tip_file_map(conn, right) == right_files
    assert find_tip(conn, root) == right
    assert find_tip(conn, left) == noop
    continued = _append(conn, run, right, right_files, right)
    assert find_tip(conn, root) == continued
    assert find_tip(conn, right) == continued
    assert not conn.execute('SELECT 1 FROM cell_version_heads WHERE cell_id=?', (right,)).fetchone()
    conn.close()


def test_reseed_appends_without_rewriting_inherited_history__P4F03(fresh_cwd):
    from cas import store_bytes
    from database import get_db_connection
    from genesis import seed_cell_from_vfiles
    from models import VirtualFile
    from versions import find_tip, tip_file_map
    conn = get_db_connection(_channel_id('reseed'))
    run, root = _root(conn, {'inherited':'original', 'removed':'old'})
    original = tip_file_map(conn, root)
    descendant_files = {**original, 'extra':store_bytes(b'descendant')}
    descendant = _append(conn, run, root, descendant_files, root)
    replacement = [VirtualFile(name='inherited', content='new seed')]
    seed_cell_from_vfiles(conn, run, root, 'Seed again', 20, replacement)
    new_tip = find_tip(conn, root)
    assert new_tip != descendant
    assert tip_file_map(conn, root) == original
    assert tip_file_map(conn, descendant) == descendant_files
    assert tip_file_map(conn, new_tip) == {'inherited':store_bytes(b'new seed')}
    seed_cell_from_vfiles(conn, run, root, 'Same seed', 21, replacement)
    assert find_tip(conn, root) == new_tip
    conn.commit()
    conn.close()


def test_direct_edge_invalidation_rolls_back_and_reopens__P4F03(fresh_cwd):
    from database import get_db_connection
    from versions import find_tip
    conn = get_db_connection(_channel_id('edge-rollback'))
    run, root = _root(conn)
    tip = _append(conn, run, root, {}, root)
    _, sibling = _root(conn)
    conn.commit()
    conn.execute("INSERT INTO cell_edges VALUES (?,?,'previous_version')", (root, sibling))
    assert find_tip(conn, root) == sibling
    assert not conn.execute('SELECT 1 FROM cell_version_heads').fetchone()
    conn.rollback()
    assert find_tip(conn, root) == tip
    assert conn.execute('SELECT tip_id FROM cell_version_heads WHERE cell_id=?', (root,)).fetchone()[0] == tip
    conn.execute("UPDATE cell_edges SET child_cell_id=? WHERE parent_cell_id=? AND parent_key='previous_version'", (sibling, root))
    conn.commit()
    conn.close()
    conn = get_db_connection(_channel_id('edge-rollback'))
    assert find_tip(conn, root) == sibling
    assert find_tip(conn, tip) == tip
    # Owner reseeding follows the rewritten edge, then ordinary appends resume.
    final = _append(conn, run, sibling, {}, root)
    assert find_tip(conn, root) == final
    conn.commit()
    conn.close()


def test_long_chain_gc_sweep_and_cold_costs__P4F03_O02(fresh_cwd, monkeypatch):
    import json
    import os
    import time
    import cas
    import cas_gc
    import state
    from database import get_db_connection
    from models import AppState
    from versions import find_tip, tip_file_map
    monkeypatch.setattr(state, 'state', AppState())
    conn = get_db_connection(_channel_id('gc-chain'))
    run, root = _root(conn, {'inherited':'retained', 'removed':'only in the root'})
    files = tip_file_map(conn, root)
    removed = files.pop('removed')
    orphan = cas.store_bytes(b'referenced by no version')
    tip = root
    measurements = []
    for i in range(500):
        files = {**files, 'changing':cas.store_bytes(str(i).encode())}
        tip = _append(conn, run, tip, files, root)
        if i in (19, 199, 499):
            # Force the cold historical path without introducing a snapshot.
            cold = tip
            following = _append(conn, run, tip, files, root)
            calls = []
            conn.set_trace_callback(calls.append)
            started = time.perf_counter()
            assert tip_file_map(conn, cold) == files
            elapsed = time.perf_counter() - started
            conn.set_trace_callback(None)
            measurements.append({'appends':i+1, 'cold_statements':len(calls), 'cold_seconds':round(elapsed,6)})
            tip = following
    conn.commit()
    conn.close()
    for digest in (orphan, removed, *files.values()):
        os.utime(cas.blob_path(digest), (0, 0))
    started = time.perf_counter()
    result = cas_gc.run_gc(grace_seconds=0)
    assert not result.get('aborted')
    assert result['swept'] >= 1
    assert cas.read_blob(orphan) is None
    # Every version is live: a file only an old version holds is kept.
    assert cas.read_blob(removed) is not None
    assert all(cas.read_blob(digest) is not None for digest in files.values())
    conn = get_db_connection(_channel_id('gc-chain'))
    assert find_tip(conn, root) == tip
    assert tip_file_map(conn, tip) == files
    conn.close()
    print('P4F03_COLD_AND_GC=' + json.dumps({'cold':measurements, 'gc_seconds':round(time.perf_counter()-started,6), 'versions':504}, sort_keys=True))



def test_gc_missing_version_metadata_aborts_before_sweep__P4F03(fresh_cwd, monkeypatch):
    import os
    import cas
    import cas_gc
    import state
    from database import get_db_connection
    from models import AppState
    from versions import tip_file_map
    monkeypatch.setattr(state, 'state', AppState())
    conn = get_db_connection(_channel_id('corrupt-metadata'))
    _, root = _root(conn, {'retained':'must not collect'})
    digest = tip_file_map(conn, root)['retained']
    conn.execute('DELETE FROM cell_file_versions WHERE version_id=?', (root,))
    conn.commit()
    conn.close()
    os.utime(cas.blob_path(digest), (0, 0))
    result = cas_gc.run_gc(grace_seconds=0)
    assert result['aborted'] and result['swept'] == result['purged_sweeps'] == 0
    assert cas.read_blob(digest) == b'must not collect'


def test_selected_reads_are_indexed_atomic_and_tombstone_aware__P4F03(fresh_cwd):
    import json
    from cas import store_bytes
    from database import get_db_connection
    from versions import tip_file_map, version_file_hashes
    measures = []
    for count in (20, 200, 2000):
        conn = get_db_connection(_channel_id(f'selected-{count}'))
        run, root = _root(conn, {f'f{i}':str(i) for i in range(count)})
        original = tip_file_map(conn, root)
        steps = []
        conn.set_progress_handler(lambda: steps.append(1) or 0, 1)
        assert version_file_hashes(conn, root, ['f0']) == {'f0':original['f0']}
        conn.set_progress_handler(None, 0)
        measures.append({'files':count, 'selected_vm_steps':len(steps)})
        assert len(steps) < 100
        changed = {**original, 'f0':store_bytes(b'new')}
        changed.pop('f1')
        tip = _append(conn, run, root, changed, root)
        later = _append(conn, run, tip, {**changed, 'f1':store_bytes(b'readded')}, root)
        assert version_file_hashes(conn, root, ['f0','f1','absent']) == {p:original[p] for p in ('f0','f1')}
        assert version_file_hashes(conn, tip, ['f0','f1','absent']) == {'f0':changed['f0']}
        assert version_file_hashes(conn, later, ['absent']) == {}
        assert version_file_hashes(conn, later, []) == {}
        conn.commit()
        conn.close()
    print('P4F03_SELECTED_READS=' + json.dumps(measures))

    reader, writer = get_db_connection(_channel_id('selected-race')), get_db_connection(_channel_id('selected-race'))
    run, root = _root(writer, {'a':'old'})
    writer.commit()
    original = tip_file_map(reader, root)
    class RacingReader:
        raced = False
        def execute(self, sql, parameters=()):
            cursor = reader.execute(sql, parameters)
            if 'cell_file_heads' in sql and not self.raced:
                self.raced = True
                _append(writer, run, root, {'a':store_bytes(b'new')}, root)
                writer.commit()
            return cursor
    assert version_file_hashes(RacingReader(), root, ['a']) == original
    reader.close()
    writer.close()


def test_missing_existing_snapshot_metadata_is_not_empty__P4F03(fresh_cwd):
    import pytest
    from database import get_db_connection
    from versions import tip_file_map, version_file_hashes
    conn = get_db_connection(_channel_id('missing-cold'))
    run, root = _root(conn, {'a':'old'})
    _append(conn, run, root, {}, root)
    conn.execute('DELETE FROM cell_file_versions WHERE version_id=?', (root,))
    for read in (lambda version: tip_file_map(conn, version),
                 lambda version: version_file_hashes(conn, version, ['a'])):
        with pytest.raises(ValueError, match='missing file metadata'):
            read(root)
        assert read('unknown') == {}
    conn.close()
