"""Conditional file mutations and pinned readers reject malformed CAS keys."""
from __future__ import annotations

import json
from pathlib import Path
import re
import uuid

import pytest

from import_cells import import_cell

from cas import is_cas_hash


@pytest.mark.parametrize('value',[
    'zz/etc/passwd','../../../etc/passwd','ab/../../../etc/passwd',
    'a'*63,'a'*65,'A'*64,'g'*64,'',
])
def test_malformed_hashes_are_rejected__P6F01(value):
    assert not is_cas_hash(value)


def test_the_real_digest_shape_is_accepted__P6F01():
    assert is_cas_hash('a'*64)
    assert is_cas_hash('0123456789abcdef'*4)


@pytest.fixture
def target(client):
    from state import get_active_channel
    channel=str(get_active_channel().id)
    made=import_cell(client,channel,[{'name':'source.txt','content':'original'}],name='Binary persistence')
    assert made.status_code==200,made.text
    return client,channel,made.json()['cell_ids'][0]


def write(target,path,**body):
    client,channel,cell=target
    return client.post(f'/api/channels/{channel}/cells/{cell}/files/mutate',json={
        'operation_id':str(uuid.uuid4()),'mutations':[{
            'path':path,'base_revision':0,'writer_id':'binary-test','edit_seq':1,'kind':'put',**body}],
        'resolves':[]})


def test_mutation_refuses_a_traversing_blob_hash(target):
    response=write(target,'data.bin',blob_hash='zz/etc/passwd',encoding='binary')
    assert response.status_code==422,response.text
    assert 'Invalid CAS hash' in response.text


def test_text_encoding_refuses_a_non_utf8_admitted_body(target):
    from cas import store_bytes
    response=write(target,'latin1.txt',blob_hash=store_bytes(b'caf\xe9'),encoding='utf-8')
    assert response.status_code==400,response.text
    assert 'UTF-8' in response.text


@pytest.mark.parametrize('raw',[b'',b'\x00\xffvalid binary bytes'])
def test_accepted_binary_keeps_encoding_and_exact_bytes(target,raw):
    from cas import store_bytes
    response=write(target,'data.bin',blob_hash=store_bytes(raw),encoding='binary')
    assert response.status_code==200,response.text
    file=response.json()['files'][0]
    assert file['encoding']=='binary' and file['body_hash']==store_bytes(raw)
    client,channel,cell=target
    read=client.get(f'/api/channels/{channel}/cells/{cell}/files/data.bin',params={'revision':file['file_revision']})
    assert read.content==raw
    assert read.headers['x-file-encoding']=='binary'


def test_poisoned_sql_hash_cannot_reach_an_arbitrary_file(target,tmp_path):
    from database import get_db_connection
    client,channel,cell=target
    secret=tmp_path/'secret.txt'
    secret.write_text('SECRET')
    conn=get_db_connection(channel)
    try:
        conn.execute('UPDATE file_revisions SET body_hash=? WHERE cell_id=? AND path=?',
                     (f'zz{secret}',cell,'source.txt'))
        conn.commit()
    finally:conn.close()
    response=client.get(f'/api/channels/{channel}/cells/{cell}/files/source.txt')
    assert response.status_code==409,response.text
    assert 'SECRET' not in response.text


def test_memory_hash_is_not_a_current_body_authority(target,tmp_path):
    from cell_placement import find_cell
    from state import require_channel
    client,channel,cell=target
    secret=tmp_path/'secret.txt'
    secret.write_text('SECRET')
    _,found=find_cell(require_channel(channel),cell)
    found.files[0].body_hash=f'zz{secret}'
    found.files[0].content='incorrect memory bytes'
    response=client.get(f'/api/channels/{channel}/cells/{cell}/files/source.txt')
    assert response.status_code==200,response.text
    assert response.content==b'original'


def test_documented_staged_upload_is_readable_before_history_commit__D12(target):
    """Execute the guide's exact accepted-versus-checkpoint visibility contract."""
    import collimate
    from cas import store_bytes
    from database import get_db_connection
    from versions import find_tip,tip_file_map
    root=Path(collimate.__file__).resolve().parents[2]
    guide=(root/'shared/docs/how-to-write-a-renderer.md').read_text()
    raw,=re.findall(r'^```json example=renderer-upload-readback\n(.*?)^```$',guide,re.M|re.S)
    example=json.loads(raw)
    data=example['text'].encode('utf-8')
    response=write(target,example['path'],blob_hash=store_bytes(data),encoding='binary')
    assert (response.status_code==200 and response.json()['outcome']=='accepted') is example['acceptedBeforeCheckpoint']
    client,channel,cell=target
    read=client.get(f'/api/channels/{channel}/cells/{cell}/files/'+example['path'])
    assert (read.status_code==200 and read.content==data) is example['visibleBeforeCheckpoint']
    conn=get_db_connection(channel)
    try:
        tip=find_tip(conn,cell)
        in_history=bool(tip and example['path'] in tip_file_map(conn,tip))
    finally:conn.close()
    assert in_history is example['historyBeforeCheckpoint']
