"""Explicit snapshot types and collectible revisions with permanent receipts."""
import importlib
import json
import uuid

import pytest


@pytest.fixture
def storage(fresh_cwd):
    from conftest import reset_api_modules
    reset_api_modules()
    import persistence
    from database import get_db_connection
    from models import Channel, Cell, Stack
    from state import state
    cell = Cell(name='Synthetic storage')
    channel = Channel(name='Synthetic storage', main_stacks=[Stack(cells=[cell])])
    state.channels = [channel]
    conn = get_db_connection(str(channel.id))
    yield persistence, channel, str(cell.id), conn
    conn.close()


def test_typed_history_empty_utf8_binary_and_type_only_delta(storage):
    p, channel, cell, conn = storage
    from cas import store_bytes
    from versions import synthesize_genesis_row, version_file_entries, build_virtual_files, insert_version, tip_file_map
    entries = [dict(path='empty.dat',body_hash=store_bytes(b''),encoding='binary',language='custom'),
               dict(path='utf8.dat',body_hash=store_bytes(b'valid utf8'),encoding='binary',language='custom'),
               dict(path='empty.txt',body_hash=store_bytes(b''),encoding='utf-8',language='plain')]
    p.initialize_files(conn, cell, entries)
    run = str(uuid.uuid4())
    conn.execute("INSERT INTO runs(id,status) VALUES (?,'completed')", (run,))
    synthesize_genesis_row(conn,cell,run,0,entries)
    assert version_file_entries(conn,cell) == sorted(entries,key=lambda f:f['path'])
    rebuilt = {f.name:f for f in build_virtual_files(version_file_entries(conn,cell))}
    assert rebuilt['empty.dat'].blob_hash == entries[0]['body_hash']
    assert rebuilt['utf8.dat'].blob_hash == entries[1]['body_hash'] and rebuilt['utf8.dat'].content == ''
    assert rebuilt['empty.txt'].blob_hash is None and rebuilt['empty.txt'].content == ''
    changed = [dict(f,encoding='utf-8') if f['path']=='utf8.dat' else f for f in entries]
    hashes = tip_file_map(conn,cell)
    version,_ = insert_version(conn,tip_id=cell,parent_files=hashes,files=hashes,file_entries=changed,
                               run_id=run,version_name='Type change',source_type='manual_edit',generator_name='test')
    assert conn.execute('SELECT COUNT(*) FROM cell_files WHERE cell_id=?',(version,)).fetchone()[0] == 1
    assert next(f for f in version_file_entries(conn,cell) if f['path']=='utf8.dat')['encoding']=='binary'
    assert next(f for f in version_file_entries(conn,version) if f['path']=='utf8.dat')['encoding']=='utf-8'
    # Recording history does not mutate the accepted current snapshot.
    assert next(f for f in p.snapshot_files(conn,cell) if f['path']=='utf8.dat')['encoding']=='binary'


def test_history_files_tree_preserves_snapshot_types_and_languages(storage):
    p, channel, cell, conn = storage
    from cas import store_bytes
    from versions import synthesize_genesis_row, insert_version
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.cells import router
    entries = [dict(path='empty.bin',body_hash=store_bytes(b''),encoding='binary',language='empty-custom'),
               dict(path='utf8.py',body_hash=store_bytes(b'print(1)'),encoding='binary',language='binary-custom'),
               dict(path='empty.txt',body_hash=store_bytes(b''),encoding='utf-8',language='text-custom'),
               dict(path='raw.bin',body_hash=store_bytes(b'\x00\xff'),encoding='binary',language='raw-custom')]
    p.initialize_files(conn,cell,entries)
    run = str(uuid.uuid4())
    conn.execute("INSERT INTO runs(id,status) VALUES (?,'completed')",(run,))
    synthesize_genesis_row(conn,cell,run,0,entries)
    changed = [dict(item,encoding='utf-8',language='python') if item['path']=='utf8.py' else item for item in entries]
    hashes = {item['path']:item['body_hash'] for item in entries}
    version,_ = insert_version(conn,tip_id=cell,parent_files=hashes,files=hashes,file_entries=changed,
        run_id=run,version_name='Explicit type change',source_type='manual_edit',generator_name='synthetic')
    conn.commit()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for version_id, expected in [(cell,entries),(version,changed)]:
            response = client.get(f'/api/channels/{channel.id}/cells/{version_id}/files-tree')
            assert response.status_code == 200,response.text
            actual = {item['name']:(item['body_hash'],item['encoding'],item['language']) for item in response.json()['files']}
            assert actual == {item['path']:(item['body_hash'],item['encoding'],item['language']) for item in expected}


@pytest.mark.parametrize('damage',['missing','corrupt'])
def test_history_restore_refuses_unavailable_body(storage,damage):
    p,channel,cell,conn=storage
    from cas import store_bytes,blob_path
    from pathlib import Path
    from versions import build_virtual_files
    digest=store_bytes(b'snapshot')
    path=Path(blob_path(digest))
    path.unlink() if damage=='missing' else path.write_bytes(b'changed')
    with pytest.raises(OSError):
        build_virtual_files([dict(path='a',body_hash=digest,encoding='binary',language='binary')])


def _save(p,conn,cell,seq,base,body,*,writer='writer'):
    from cas import store_bytes
    operation=p.FileOperation.model_validate(dict(operation_id=str(uuid.uuid4()),mutations=[
        dict(path='a',kind='put',blob_hash=store_bytes(body),encoding='utf-8',language='text',
             base_revision=base,writer_id=writer,edit_seq=seq)]))
    staged=p._stage(operation)
    digest=__import__('hashlib').sha256(json.dumps(operation.model_dump(exclude_unset=True),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    result=p._mutate(conn,cell,operation,staged,digest)
    return operation,result


def test_ten_thousand_saves_collect_bodies_keep_receipts_and_restart_highwaters(storage,monkeypatch):
    p,channel,cell,conn=storage
    import cas_gc
    from fastapi import HTTPException
    first=None
    for seq in range(1,10001):
        operation,result=_save(p,conn,cell,seq,seq-1,f'body {seq}'.encode())
        conn.commit()
        if seq==1: first=(operation,result)
    conn.commit()
    assert conn.execute('SELECT COUNT(*) FROM file_revisions').fetchone()[0]==10000
    assert p.prune_file_revisions(conn)==9999
    conn.commit()
    assert conn.execute('SELECT COUNT(*) FROM file_operations').fetchone()[0]==10000
    monkeypatch.setattr(cas_gc,'MIN_BLOB_AGE_SECONDS',0)
    result=cas_gc.run_gc()
    assert not result.get('aborted'),result
    assert result['swept']==9999
    measurements=dict(saves=10000,commits=10000,retained_revisions=conn.execute('SELECT COUNT(*) FROM file_revisions').fetchone()[0],
        collected_bodies=result['swept'],retained_bodies=len(list(cas_gc._iter_cas_blobs())),
        receipt_rows=conn.execute('SELECT COUNT(*) FROM file_operations').fetchone()[0],
        receipt_value_bytes=conn.execute('SELECT SUM(length(operation_id)+length(cell_id)+length(request_digest)+length(result_json)) FROM file_operations').fetchone()[0],
        path_highwater_rows=conn.execute('SELECT COUNT(*) FROM file_path_sequences').fetchone()[0],
        writer_highwater_rows=conn.execute('SELECT COUNT(*) FROM file_writer_sequences').fetchone()[0])
    print('P3_RETENTION_MEASUREMENTS='+json.dumps(measurements,sort_keys=True))
    assert p.read_file(str(channel.id),cell,'a')[1]==b'body 10000'
    with pytest.raises(HTTPException) as gone:
        p.read_file(str(channel.id),cell,'a',revision=1)
    assert gone.value.status_code==410
    # A fresh connection and service module model restart; replay never rereads collected bytes.
    importlib.reload(p)
    assert p.mutate_files(str(channel.id),cell,first[0].model_dump(exclude_unset=True))==first[1]
    from database import get_db_connection
    restarted=get_db_connection(str(channel.id),create=False)
    try:
        op,stale=_save(p,restarted,cell,9999,10000,b'late old edit')
        assert stale['outcome']=='superseded'
        _,accepted=_save(p,restarted,cell,10001,10000,b'latest')
        restarted.commit()
        assert accepted['files'][0]['file_revision']==10001
        assert restarted.execute('SELECT COUNT(*) FROM file_revisions').fetchone()[0]==2
    finally:
        restarted.close()


def test_unresolved_conflict_comparison_and_body_pin_are_roots(storage):
    p,channel,cell,conn=storage
    from cas import pin_blobs
    _,a=_save(p,conn,cell,1,0,b'base')
    _,b=_save(p,conn,cell,2,1,b'current')
    _,conflict=_save(p,conn,cell,1,1,b'candidate',writer='other')
    _,latest=_save(p,conn,cell,3,2,b'new current')
    conn.commit()
    assert p.prune_file_revisions(conn)==0
    conflict_id=conflict['conflicts'][0]['conflict_id']
    conn.execute('UPDATE file_conflicts SET resolution_operation_id=? WHERE conflict_id=?',('resolved',conflict_id))
    digest=conn.execute('SELECT body_hash FROM file_revisions WHERE revision=2').fetchone()[0]
    with pin_blobs([digest]):
        assert p.prune_file_revisions(conn)==2
        assert conn.execute('SELECT revision FROM file_revisions ORDER BY revision').fetchall()[0][0]==2
    assert p.prune_file_revisions(conn)==1
    conn.commit()


def test_one_time_history_conversion_freezes_old_interpretation_and_exact_shelf_type(fresh_cwd):
    import sqlite3
    import migrations
    from cas import store_bytes
    from versions import version_file_entries, build_virtual_files
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    migrations.apply(conn,migrations.CHANNEL_MIGRATIONS[:22])
    raw=store_bytes(b'UTF8-compatible binary')
    conn.execute("INSERT INTO blobs(hash,size) VALUES (?,?)",(raw,22))
    for cell in ('without-type-evidence','exact-shelf'):
        conn.execute('INSERT INTO cells(id) VALUES (?)',(cell,))
        conn.execute('INSERT INTO cell_files(cell_id,filepath,blob_hash) VALUES (?,?,?)',(cell,'a.dat',raw))
        conn.execute('INSERT INTO cell_file_current(root_id,filepath,blob_hash) VALUES (?,?,?)',(cell,'a.dat',raw))
        conn.execute('INSERT INTO cell_file_heads(root_id,version_id) VALUES (?,?)',(cell,cell))
    conn.execute("INSERT INTO removed_cells(cell_id,name,stack_cell_count,removed_at,metadata_json,binary_paths_json,reason) VALUES ('exact-shelf','Shelf',1,'now','{}','[\"a.dat\"]','removed')")
    conn.commit()
    assert migrations.apply(conn,migrations.CHANNEL_MIGRATIONS)==[23,24,25,26,27,28,29,30]
    assert 'binary_paths_json' not in {r[1] for r in conn.execute('PRAGMA table_info(removed_cells)')}
    assert version_file_entries(conn,'without-type-evidence')[0]['encoding']=='utf-8'
    assert version_file_entries(conn,'exact-shelf')[0]['encoding']=='binary'
    assert build_virtual_files(version_file_entries(conn,'exact-shelf'))[0].blob_hash==raw
    with pytest.raises(sqlite3.IntegrityError,match='explicit type'):
        conn.execute('INSERT INTO cell_files(cell_id,filepath,blob_hash) VALUES (?,?,?)',('exact-shelf','bad',raw))
    conn.rollback()
    assert migrations.apply(conn,migrations.CHANNEL_MIGRATIONS)==[]
    conn.close()


@pytest.mark.parametrize('damage',['missing','corrupt'])
def test_history_conversion_missing_body_rolls_back_entire_migration(fresh_cwd,damage):
    import sqlite3
    import migrations
    from pathlib import Path
    from cas import store_bytes,blob_path
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    migrations.apply(conn,migrations.CHANNEL_MIGRATIONS[:22])
    digest=store_bytes(b'required')
    conn.execute('INSERT INTO cells(id) VALUES (?)',('history',))
    conn.execute('INSERT INTO blobs(hash,size) VALUES (?,?)',(digest,8))
    conn.execute('INSERT INTO cell_files(cell_id,filepath,blob_hash) VALUES (?,?,?)',('history','a',digest))
    conn.commit()
    path=Path(blob_path(digest))
    path.unlink() if damage=='missing' else path.write_bytes(b'corruption')
    with pytest.raises(OSError):
        migrations.apply(conn,migrations.CHANNEL_MIGRATIONS)
    assert 'encoding' not in {r[1] for r in conn.execute('PRAGMA table_info(cell_files)')}
    assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==22
    conn.close()


def test_absent_path_conflict_and_delete_recreate_never_reuse_revisions(storage):
    p,channel,cell,conn=storage
    operation,conflict=_save(p,conn,cell,4,99,b'conflict on absent path')
    assert conflict['outcome']=='conflict'
    assert not p.snapshot_files(conn,cell)
    conn.execute("UPDATE file_conflicts SET resolution_operation_id='resolved'")
    assert p.prune_file_revisions(conn)==1
    _,accepted=_save(p,conn,cell,5,0,b'accepted after conflict')
    assert accepted['files'][0]['file_revision']==2
    delete=p.FileOperation.model_validate(dict(operation_id=str(uuid.uuid4()),mutations=[
        dict(path='a',kind='delete',writer_id='writer',edit_seq=6,base_revision=2)]))
    p._mutate(conn,cell,delete,p._stage(delete),'delete-digest')
    assert p.prune_file_revisions(conn)==1
    assert p.snapshot_files(conn,cell,include_deleted=True)[0]['file_revision']==3
    _,recreated=_save(p,conn,cell,7,3,b'recreated')
    assert recreated['files'][0]['file_revision']==4
    _,stale=_save(p,conn,cell,5,4,b'late')
    assert stale['outcome']=='superseded'
    assert conn.execute('SELECT last_allocated_revision FROM file_path_sequences').fetchone()[0]==4
    conn.commit()


def test_resolved_revision_read_finishes_before_collection_can_remove_its_bytes(storage,monkeypatch):
    import concurrent.futures
    import threading
    import cas
    import cas_gc
    p,channel,cell,conn=storage
    _,first=_save(p,conn,cell,1,0,b'old captured body')
    _,second=_save(p,conn,cell,2,1,b'new current body')
    conn.commit()
    entered,release=threading.Event(),threading.Event()
    read=cas.read_blob
    old_hash=first['files'][0]['body_hash']
    def paused(digest):
        if digest==old_hash:
            entered.set()
            assert release.wait(10)
        return read(digest)
    monkeypatch.setattr(cas,'read_blob',paused)
    monkeypatch.setattr(cas_gc,'MIN_BLOB_AGE_SECONDS',0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        reading=pool.submit(p.read_file,str(channel.id),cell,'a',revision=1)
        assert entered.wait(10)
        collecting=pool.submit(cas_gc.run_gc)
        release.set()
        assert reading.result(timeout=10)[1]==b'old captured body'
        assert not collecting.result(timeout=10).get('aborted')


@pytest.mark.parametrize('source',['workspace','runner','generator'])
@pytest.mark.parametrize('damage',['missing','corrupt'])
def test_required_staging_body_refuses_missing_or_corrupt_bytes(storage,tmp_path,source,damage):
    _,channel,cell,conn=storage
    from pathlib import Path
    from cas import store_bytes,blob_path
    from versions import synthesize_genesis_row
    from models import VirtualFile
    import run_staging
    name='runner/files/scripts/input.bin' if source=='runner' else 'input.bin'
    digest=store_bytes(b'required binary')
    entries=[dict(path=name,body_hash=digest,encoding='binary',language='custom')]
    run=str(uuid.uuid4())
    conn.execute("INSERT INTO runs(id,status) VALUES (?,'completed')",(run,))
    synthesize_genesis_row(conn,cell,run,0,entries)
    conn.commit()
    body=Path(blob_path(digest))
    body.unlink() if damage=='missing' else body.write_bytes(b'corruption')
    workspace=tmp_path/'staged'
    workspace.mkdir()
    with pytest.raises(OSError,match='input.bin'):
        if source=='workspace':
            run_staging.hydrate_workspace(str(channel.id),cell,str(workspace),tip_id=cell)
        elif source=='runner':
            run_staging.stage_runner_files(str(channel.id),cell,'runner/',['scripts'],str(workspace))
        else:
            run_staging.materialize_generator_source(('generator',[('generator/input.bin',
                VirtualFile(name='input.bin',blob_hash=digest))]),str(workspace))
    assert not list(workspace.rglob('input.bin'))


def test_required_staging_preserves_empty_binary_and_executable_mode(storage,tmp_path):
    _,channel,cell,conn=storage
    import os
    import stat
    from cas import store_bytes,blob_path
    from versions import synthesize_genesis_row
    from run_staging import hydrate_workspace
    digest=store_bytes(b'')
    os.chmod(blob_path(digest),0o755)
    run=str(uuid.uuid4())
    conn.execute("INSERT INTO runs(id,status) VALUES (?,'completed')",(run,))
    synthesize_genesis_row(conn,cell,run,0,[dict(path='empty.bin',body_hash=digest,encoding='binary',language='custom')])
    conn.commit()
    hydrate_workspace(str(channel.id),cell,str(tmp_path),tip_id=cell)
    target=tmp_path/'empty.bin'
    assert target.read_bytes()==b''
    assert stat.S_IMODE(target.stat().st_mode)==0o755


@pytest.mark.parametrize('damage',['missing','corrupt'])
def test_dead_run_sweep_rolls_back_every_capture_when_later_body_fails(storage,monkeypatch,damage):
    p,channel,cell,conn=storage
    from pathlib import Path
    from cas import blob_path,store_bytes,require_blob
    from cell_commits import commit_cell_change,sweep_dead_run_folders
    import run_artifacts
    run=str(uuid.uuid4())
    conn.execute("INSERT INTO runs(id,status) VALUES (?,'completed')",(run,))
    conn.commit()
    prefix=f'_runs/synthetic-{run[:8]}'
    first,last=f'{prefix}/a.txt',f'{prefix}/z.txt'
    commit_cell_change(str(channel.id),cell,run,writes={first:'checkpoint first',last:'checkpoint last'})
    # These newer accepted bodies have not become another explicit checkpoint.
    bodies={first:b'latest accepted first',last:b'latest accepted last'}
    p.restore_snapshot(conn,cell,[dict(path=path,body_hash=store_bytes(raw),encoding='utf-8',language='text')
                                  for path,raw in bodies.items()])
    conn.commit()
    before=p.snapshot_files(conn,cell)
    versions=conn.execute('SELECT COUNT(*) FROM cells').fetchone()[0]
    damaged=next(item['body_hash'] for item in before if item['path']==last)
    path=Path(blob_path(damaged))
    captures=[]
    record=run_artifacts.record_artifact_blob
    def capture(*args):
        captures.append(args[3])
        if args[3]==last:
            path.unlink() if damage=='missing' else path.write_bytes(b'corrupt capture')
        return record(*args)
    monkeypatch.setattr(run_artifacts,'record_artifact_blob',capture)
    with pytest.raises(OSError,match='z.txt'):
        sweep_dead_run_folders(str(channel.id),cell)
    assert captures==[first,last]
    assert p.snapshot_files(conn,cell)==before
    assert conn.execute('SELECT COUNT(*) FROM cells').fetchone()[0]==versions
    assert conn.execute('SELECT COUNT(*) FROM run_artifacts WHERE run_id=?',(run,)).fetchone()[0]==0
    assert require_blob(before[0]['body_hash'])==bodies[first]
    # Restoring the exact required object allows the same sweep to archive
    # both current bodies and atomically publish their tombstones.
    path.write_bytes(bodies[last])
    monkeypatch.setattr(run_artifacts,'record_artifact_blob',record)
    assert sweep_dead_run_folders(str(channel.id),cell)
    assert p.snapshot_files(conn,cell)==[]
    logs={row['filepath']:require_blob(row['blob_hash']) for row in conn.execute(
        'SELECT filepath,blob_hash FROM run_artifacts WHERE run_id=?',(run,))}
    assert logs==bodies


def test_sweep_preserves_newer_accepted_body_beside_explicit_log_after_gc_restart(storage,monkeypatch):
    p,channel,cell,conn=storage
    import os
    from pathlib import Path
    import subprocess
    import sys
    import cas_gc
    from cas import store_bytes,require_blob
    from cell_commits import commit_cell_change,sweep_dead_run_folders
    from run_artifacts import record_artifact_bytes
    run=str(uuid.uuid4())
    conn.execute("INSERT INTO runs(id,status) VALUES (?,'completed')",(run,))
    conn.commit()
    path=f'_runs/synthetic-{run[:8]}/output.bin'
    commit_cell_change(str(channel.id),cell,run,writes={path:b'checkpoint bytes'})
    record_artifact_bytes(conn,run,'log',path,b'deliberate log')
    latest=store_bytes(b'new accepted binary')
    p.restore_snapshot(conn,cell,[dict(path=path,body_hash=latest,encoding='binary',language='custom')])
    conn.commit()
    assert sweep_dead_run_folders(str(channel.id),cell)
    assert p.snapshot_files(conn,cell)==[]
    log=conn.execute('SELECT blob_hash FROM run_artifacts WHERE run_id=? AND filepath=?',(run,path)).fetchone()[0]
    assert require_blob(log)==b'deliberate log'
    monkeypatch.setattr(cas_gc,'MIN_BLOB_AGE_SECONDS',0)
    assert not cas_gc.run_gc().get('aborted')
    # A fresh process resolves history and required bytes from this database.
    script='''import json,sys
from database import get_db_connection
from versions import version_file_entries
from cas import require_blob
conn=get_db_connection(sys.argv[1],create=False)
entries=[entry for (version,) in conn.execute('SELECT id FROM cells') for entry in version_file_entries(conn,version) if entry['path']==sys.argv[2]]
print(json.dumps([dict(entry,raw=require_blob(entry['body_hash']).hex()) for entry in entries]))
'''
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    recovered=json.loads(subprocess.check_output([sys.executable,'-c',script,str(channel.id),path],env=env,text=True))
    assert any(entry['body_hash']==latest and entry['encoding']=='binary' and entry['language']=='custom'
               and entry['raw']==b'new accepted binary'.hex() for entry in recovered)
