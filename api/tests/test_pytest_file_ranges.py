"""Range reads use the exact accepted revision, including its receipt headers."""
import hashlib
import uuid

from import_cells import import_cell


def test_byte_ranges_keep_the_pinned_body_and_metadata(client):
    from state import get_active_channel
    channel = str(get_active_channel().id)
    original = 'é-original-body'.encode('utf-8')
    made = import_cell(client, channel, [{'name': 'data.txt', 'content': original.decode()}], name='Range fixture')
    assert made.status_code == 200, made.text
    cell = made.json()['cell_ids'][0]
    url = f'/api/channels/{channel}/cells/{cell}/files/data.txt'
    baseline = client.get(url)
    revision = int(baseline.headers['x-file-revision'])
    digest = hashlib.sha256(original).hexdigest()
    changed = client.post(f'/api/channels/{channel}/cells/{cell}/files/mutate', json={
        'operation_id': str(uuid.uuid4()), 'mutations': [{
            'path': 'data.txt', 'kind': 'put', 'content_text': 'new-current-body',
            'writer_id': 'range-test', 'edit_seq': 1, 'base_revision': revision,
        }],
    })
    assert changed.status_code == 200 and changed.json()['outcome'] == 'accepted', changed.text
    for requested, start, end in [('bytes=0-3', 0, 4), ('bytes=4-', 4, len(original)),
                                  ('bytes=-4', len(original) - 4, len(original)),
                                  ('bytes=0-9999', 0, len(original))]:
        response = client.get(url, params={'revision': revision}, headers={'Range': requested})
        assert response.status_code == 206, response.text
        assert response.content == original[start:end]
        assert response.headers['content-range'] == f'bytes {start}-{end - 1}/{len(original)}'
        assert response.headers['accept-ranges'] == 'bytes'
        assert response.headers['content-length'] == str(end - start)
        for header in ['x-file-revision', 'x-body-hash', 'x-writer-id', 'x-edit-seq',
                       'x-file-encoding', 'cache-control', 'x-content-type-options']:
            assert response.headers[header] == baseline.headers[header]
    conditional = client.get(url, params={'revision': revision}, headers={
        'Range': 'bytes=0-1', 'If-Range': f'"{digest}"',
    })
    assert conditional.status_code == 206 and conditional.content == original[:2]
    mismatched = client.get(url, headers={'Range': 'bytes=0-1', 'If-Range': f'"{digest}"'})
    assert mismatched.status_code == 200 and mismatched.content == b'new-current-body'
    invalid = client.get(url, params={'revision': revision}, headers={'Range': 'bytes=9999-'})
    assert invalid.status_code == 416
    assert invalid.headers['content-range'] == f'bytes */{len(original)}'
    assert invalid.headers['x-body-hash'] == digest
    malformed = client.get(url, params={'revision': revision}, headers={'Range': 'bytes=nonsense'})
    assert malformed.status_code == 400
    multipart = client.get(url, params={'revision': revision}, headers={'Range': 'bytes=0-1,4-5'})
    assert multipart.status_code == 206
    assert multipart.headers['content-type'].startswith('multipart/byteranges; boundary=')
    for start, end in [(0, 2), (4, 6)]:
        assert f'Content-Range: bytes {start}-{end - 1}/{len(original)}'.encode() in multipart.content
        assert b'\r\n\r\n' + original[start:end] + b'\r\n' in multipart.content


def test_empty_accepted_body_refuses_unsatisfiable_range(client):
    from state import get_active_channel
    channel = str(get_active_channel().id)
    made = import_cell(client, channel, [{'name': 'empty.txt', 'content': ''}], name='Empty range fixture')
    assert made.status_code == 200, made.text
    url = f"/api/channels/{channel}/cells/{made.json()['cell_ids'][0]}/files/empty.txt"
    full = client.get(url)
    assert full.status_code == 200 and full.content == b''
    response = client.get(url, headers={'Range': 'bytes=0-'})
    assert response.status_code == 416 and response.headers['content-range'] == 'bytes */0'
