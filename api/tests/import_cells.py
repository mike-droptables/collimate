"""Make cells the way the app imports files: stream each body to
``POST /api/channels/{channel}/blob/stream``, then commit through ``POST …/import``.

Synthetic fixtures only; callers pass hand-made names and bodies.
"""
from __future__ import annotations


def upload(client, body: bytes, channel=None) -> dict:
    """Stream ``body`` into a channel's body store (the active channel's
    unless ``channel`` is given)."""
    if channel is None:
        from collimate_server.state import get_active_channel
        channel = get_active_channel().id
    response = client.post(f'/api/channels/{channel}/blob/stream', content=body)
    assert response.status_code == 200, response.text
    return response.json()


def import_cell(client, channel, files, *, name='Imported cell', stack='', insert_index=None, dirs=()):
    """One new stack holding one new cell; ``files`` are ``{'name', 'content'}``
    (text or bytes) or ``{'name', 'blob_hash'}`` for a body already stored.
    Returns the import response; its JSON carries ``stack_ids`` and ``cell_ids``."""
    refs = []
    for item in files:
        digest = item.get('blob_hash')
        if digest is None:
            content = item['content']
            digest = upload(client, content.encode('utf-8') if isinstance(content, str) else content, channel)['blob_hash']
        refs.append({'path': item['name'], 'blob_hash': digest})
    return client.post(f'/api/channels/{channel}/import', json={
        'insert_index': insert_index,
        'stacks': [{'name': stack, 'cells': [{'name': name, 'files': refs, 'dirs': list(dirs)}]}],
    })
