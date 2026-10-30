"""Export: the frozen snapshot, the folder tree, the manifest and the zip.

A scoped ``export_snapshot`` locks only the selected cells while it freezes
and hands an explicit object (descriptors and a pins lease) to the writer;
``freeze_tag_cells`` freezes a tag's live cells and its removed cells (their
version tip) the same way. The tree is ``<cell>/…``, ``<stack>/<cell>/…``,
``<channel>/<stack>/<cell>/…`` (all and main), ``<channel>/Capabilities/
<cell>/…`` (packages) or ``<tag>/<cell>/…`` with OS-safe, deduplicated
names; ``_runs/`` is skipped and a ``.collkeep`` marker becomes its empty
folder. The zip streams from pinned CAS files through a bounded queue and
releases the pins however it ends. The channel outline lists the capability
cells and main stacks by name.

Synthetic fixtures only — hand-made cells, names and bodies.
"""
from __future__ import annotations

import hashlib
import io
import os
import threading
import time
import tracemalloc
import uuid
import zipfile

import pytest

from import_cells import import_cell, upload

PNG = b'\x89PNG\r\n\x1a\n\x00\x00binary\xff'


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from database import get_db_connection
    from models import Channel
    from state import state

    with TestClient(api_app) as http:
        channel = Channel(name='My: Channel')
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        http.channel_id = str(channel.id)
        yield http


def _host_put(channel, cell_id, path, content):
    from persistence import mutate_files
    result = mutate_files(channel, cell_id, {'operation_id': str(uuid.uuid4()), 'mutations': [
        {'path': path, 'kind': 'put', 'content_text': content, 'writer_id': 'host:test',
         'edit_seq': 1, 'base_revision': 0}]}, actor='host')
    assert result['outcome'] == 'accepted', result


@pytest.fixture
def board(client):
    """Stack ``Docs`` with two cells named ``Notes`` (one with a run folder,
    a marker folder and colliding names), stack ``A:B`` with cell ``CON``, and
    the generator cell ``Tool`` on the hidden Capabilities stack."""
    from cell_placement import capabilities_stack
    from genesis import ensure_capabilities_stack
    from state import require_channel, state_lock
    channel = client.channel_id
    png = upload(client, PNG)['blob_hash']
    first = import_cell(client, channel, [
        {'name': 'a.txt', 'content': 'alpha'},
        {'name': 'sub/b.md', 'content': '# b'},
        {'name': 'img.png', 'blob_hash': png},
        {'name': 'Read.md', 'content': 'upper'},
        {'name': 'read.md', 'content': 'lower'},
        {'name': 'odd:name?.txt', 'content': 'odd'},
    ], name='Notes', stack='Docs', dirs=['empty']).json()
    notes = first['cell_ids'][0]
    _host_put(channel, notes, '_runs/r-1/out.txt', 'run output')
    docs = first['stack_ids'][0]
    second = client.post(f'/api/channels/{channel}/import', json={'stacks': [
        {'name': 'Docs', 'cells': [{'name': 'Notes', 'files': [], 'dirs': []}]}]}).json()
    twin = second['cell_ids'][0]
    # Move the twin into the first Docs stack so the names collide as siblings.
    with state_lock:
        ch = require_channel(channel)
        source = next(s for s in ch.main_stacks if str(s.id) == second['stack_ids'][0])
        target = next(s for s in ch.main_stacks if str(s.id) == docs)
        target.cells.append(source.cells.pop())
        ch.main_stacks.remove(source)
    other = import_cell(client, channel, [{'name': 'c.txt', 'content': 'gamma'}], name='CON', stack='A:B').json()
    gen = import_cell(client, channel, [{'name': 'tool.generator', 'content': 'id: tool'}], name='Tool').json()
    with state_lock:
        ch = require_channel(channel)
        stack = next(s for s in ch.main_stacks if str(s.id) == gen['stack_ids'][0])
        ch.main_stacks.remove(stack)
        ensure_capabilities_stack(ch)
        capabilities_stack(ch).cells.extend(stack.cells)
    return {'notes': notes, 'twin': twin, 'docs': docs, 'con': other['cell_ids'][0],
            'ab': other['stack_ids'][0], 'tool': gen['cell_ids'][0], 'png': png}


def _manifest(client, **params):
    response = client.get(f'/api/channels/{client.channel_id}/export/manifest', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _paths(manifest):
    return {e['path'] for e in manifest['entries']}


def test_safe_name_covers_every_os_TR10():
    from transfer_export import safe_name
    assert safe_name('a/b\\c:d*e?f"g<h>i|j', 'x') == 'a_b_c_d_e_f_g_h_i_j'
    assert safe_name('tab\there\x7f', 'x') == 'tab_here_'
    assert safe_name('trailing. . ', 'x') == 'trailing'
    assert safe_name('   ', 'Fallback') == 'Fallback'
    assert safe_name('...', 'Fallback') == 'Fallback'
    assert safe_name('CON', 'x') == 'CON_'
    assert safe_name('nul.txt', 'x') == 'nul_.txt'
    assert safe_name('lpt9.tar.gz', 'x') == 'lpt9_.tar.gz'
    assert safe_name('CONSOLE', 'x') == 'CONSOLE'


def test_manifest_cell_scope_layout_TR10(client, board):
    manifest = _manifest(client, scope='cell', cell_id=board['notes'])
    assert manifest['root'] == 'Notes'
    assert _paths(manifest) == {'Notes/a.txt', 'Notes/sub/b.md', 'Notes/img.png', 'Notes/Read.md',
                                'Notes/read (2).md', 'Notes/odd_name_.txt'}
    assert manifest['dirs'] == ['Notes/empty']
    entries = {e['path']: e for e in manifest['entries']}
    assert entries['Notes/img.png'] == {'path': 'Notes/img.png', 'body_hash': board['png'],
                                        'size': len(PNG), 'encoding': 'binary'}
    assert entries['Notes/a.txt']['encoding'] == 'utf-8'
    assert entries['Notes/a.txt']['body_hash'] == hashlib.sha256(b'alpha').hexdigest()
    assert not any('_runs' in p or '.collkeep' in p for p in _paths(manifest))


def test_manifest_stack_scope_dedupes_sibling_cells_TR10(client, board):
    manifest = _manifest(client, scope='stack', stack_id=board['docs'])
    assert manifest['root'] == 'Docs'
    assert 'Docs/Notes/a.txt' in _paths(manifest)
    # The empty twin is a folder of its own beside the first ``Notes``.
    assert manifest['dirs'] == ['Docs/Notes (2)', 'Docs/Notes/empty']


def test_manifest_all_scope_includes_nav_stacks_as_folders_TR10(client, board):
    manifest = _manifest(client, scope='all')
    assert manifest['root'] == 'My_ Channel'
    paths = _paths(manifest)
    assert 'My_ Channel/Docs/Notes/sub/b.md' in paths
    assert 'My_ Channel/A_B/CON_/c.txt' in paths
    assert 'My_ Channel/Capabilities/Tool/tool.generator' in paths
    # The app's built-in cards never travel.
    assert {p.split('/')[2] for p in paths if p.split('/')[1] == 'Capabilities'} == {'Tool'}


def test_manifest_excludes_removed_cells_TR10(client, board):
    removed = client.post(f"/api/channels/{client.channel_id}/cells/{board['con']}/remove")
    assert removed.status_code == 200, removed.text
    assert not any('/A_B/' in p for p in _paths(_manifest(client, scope='all')))
    response = client.get(f'/api/channels/{client.channel_id}/export/manifest',
                          params={'scope': 'cell', 'cell_id': board['con']})
    assert response.status_code == 404


def test_manifest_scope_arguments_are_checked_TR10(client, board):
    base = f'/api/channels/{client.channel_id}/export/manifest'
    assert client.get(base, params={'scope': 'cell'}).status_code == 400
    assert client.get(base, params={'scope': 'stack'}).status_code == 400
    assert client.get(base, params={'scope': 'cell', 'cell_id': str(uuid.uuid4())}).status_code == 404
    assert client.get(base, params={'scope': 'stack', 'stack_id': str(uuid.uuid4())}).status_code == 404
    assert client.get(base, params={'scope': 'nope'}).status_code == 422


def test_zip_holds_exact_bytes_and_folders_TR10(client, board):
    from cas import pinned_hashes
    response = client.get(f'/api/channels/{client.channel_id}/export.zip',
                          params={'scope': 'stack', 'stack_id': board['docs']})
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'application/zip'
    assert response.headers['content-disposition'].startswith('attachment; filename="Docs.zip"')
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.testzip() is None
    names = set(archive.namelist())
    assert {'Docs/Notes (2)/', 'Docs/Notes/empty/'} <= names
    assert archive.read('Docs/Notes/img.png') == PNG
    assert archive.read('Docs/Notes/a.txt') == b'alpha'
    assert archive.read('Docs/Notes/read (2).md') == b'lower'
    assert not any('_runs' in n or n.endswith('.collkeep') for n in names)
    # Written to an unseekable sink: sizes travel in data descriptors.
    assert all(info.flag_bits & 0x08 for info in archive.infolist() if not info.is_dir())
    assert pinned_hashes() == set()


def test_zip_filename_survives_non_ascii_roots_TR10(client):
    made = import_cell(client, client.channel_id, [{'name': 'x.txt', 'content': 'x'}], name='Café ✓').json()
    response = client.get(f'/api/channels/{client.channel_id}/export.zip',
                          params={'scope': 'cell', 'cell_id': made['cell_ids'][0]})
    assert response.status_code == 200
    disposition = response.headers['content-disposition']
    assert 'filename="Caf_ _.zip"' in disposition and "filename*=UTF-8''Caf%C3%A9%20%E2%9C%93.zip" in disposition


def test_cell_scope_locks_only_that_cell_TR10(client, board):
    """Freezing one cell completes while another cell's commit lock is held
    elsewhere; the whole channel would wait for it."""
    from cell_commits import cell_commit_lock
    from lifecycle_snapshots import export_snapshot
    from state import require_channel
    channel = require_channel(client.channel_id)
    held, done = threading.Event(), threading.Event()

    def hold():
        with cell_commit_lock(board['twin']):
            held.set()
            done.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        result = {}

        def freeze(cell_ids, key):
            snapshot = export_snapshot(channel, cell_ids)
            result[key] = set(snapshot.files_by_cell)
            snapshot.release()

        scoped = threading.Thread(target=freeze, args=([board['notes']], 'scoped'))
        scoped.start()
        scoped.join(5)
        assert not scoped.is_alive() and result['scoped'] == {board['notes']}
        whole = threading.Thread(target=freeze, args=(None, 'whole'))
        whole.start()
        whole.join(0.5)
        assert whole.is_alive(), 'the whole-channel freeze should wait for the held cell'
    finally:
        done.set()
        holder.join()
    whole.join(5)
    cards = {str(c.id) for s in channel.nav_stacks for c in s.cells if c.builtin}
    assert result['whole'] == {board['notes'], board['twin'], board['con'], board['tool']} | cards


def test_concurrent_save_does_not_tear_the_snapshot_TR10(client, board):
    from lifecycle_snapshots import export_snapshot
    from state import require_channel
    from transfer_export import build_tree, iter_zip
    channel = require_channel(client.channel_id)
    snapshot = export_snapshot(channel, [board['notes']])
    tree = build_tree(snapshot, 'cell', cell_id=board['notes'])
    revision = client.get(f"/api/channels/{client.channel_id}/cells/{board['notes']}/files/a.txt").headers['x-file-revision']
    saved = client.post(f"/api/channels/{client.channel_id}/cells/{board['notes']}/files/mutate", json={
        'operation_id': str(uuid.uuid4()), 'mutations': [{
            'path': 'a.txt', 'kind': 'put', 'content_text': 'saved during export', 'writer_id': 'w',
            'edit_seq': 1, 'base_revision': int(revision)}]})
    assert saved.json()['outcome'] == 'accepted', saved.text
    archive = zipfile.ZipFile(io.BytesIO(b''.join(iter_zip(tree, snapshot))))
    assert archive.read('Notes/a.txt') == b'alpha'
    fresh = {e['path']: e['body_hash'] for e in _manifest(client, scope='cell', cell_id=board['notes'])['entries']}
    assert fresh['Notes/a.txt'] == hashlib.sha256(b'saved during export').hexdigest()


def test_cell_joining_the_stack_after_selection_answers_retry_TR10(client, board, monkeypatch):
    """A cell moved into the stack between the route choosing the stack's
    cells and the freeze is not exported as an empty folder: 409, retry."""
    import lifecycle_snapshots
    from state import require_channel, state_lock
    freeze = lifecycle_snapshots.export_snapshot

    def move_then_freeze(channel, cell_ids=None):
        with state_lock:
            ch = require_channel(client.channel_id)
            source = next(s for s in ch.main_stacks if str(s.id) == board['ab'])
            target = next(s for s in ch.main_stacks if str(s.id) == board['docs'])
            target.cells.append(source.cells.pop())
        return freeze(channel, cell_ids)

    monkeypatch.setattr(lifecycle_snapshots, 'export_snapshot', move_then_freeze)
    response = client.get(f'/api/channels/{client.channel_id}/export/manifest',
                          params={'scope': 'stack', 'stack_id': board['docs']})
    assert response.status_code == 409, response.text
    assert _wait_unpinned() == set()


def _wait_unpinned(timeout=5.0):
    from cas import pinned_hashes
    deadline = time.monotonic() + timeout
    while pinned_hashes() and time.monotonic() < deadline:
        time.sleep(0.02)
    return pinned_hashes()


def test_pins_hold_while_streaming_and_release_after_TR10(client, board):
    from cas import pinned_hashes
    from lifecycle_snapshots import export_snapshot
    from state import require_channel
    from transfer_export import build_tree, iter_zip
    snapshot = export_snapshot(require_channel(client.channel_id), [board['notes']])
    pieces = iter_zip(build_tree(snapshot, 'cell', cell_id=board['notes']), snapshot)
    assert board['png'] in pinned_hashes()
    list(pieces)
    assert _wait_unpinned() == set()


def test_abandoned_stream_releases_its_pins_TR10(client):
    """A client that stops reading (the iterator is closed early) cancels
    the writer; the pins release without the rest of the archive."""
    from cas import pinned_hashes, store_stream
    from lifecycle_snapshots import export_snapshot
    from state import require_channel
    from transfer_export import ZipStream, build_tree
    block = os.urandom(64 * 1024)
    digest, _size = store_stream(block for _ in range(256))
    made = import_cell(client, client.channel_id, [{'name': 'big.bin', 'blob_hash': digest}]).json()
    snapshot = export_snapshot(require_channel(client.channel_id), made['cell_ids'])
    stream = ZipStream(build_tree(snapshot, 'cell', cell_id=made['cell_ids'][0]), snapshot)
    assert stream.next_chunk()
    assert digest in pinned_hashes()
    stream.close()
    assert stream.join(5)
    assert pinned_hashes() == set()


def test_zip_of_a_200_mib_body_streams_in_bounded_memory_TR10(client):
    """A 200 MiB body passes through the zip writer without ever being held
    whole: the traced allocation peak stays a small fraction of it."""
    from cas import store_stream
    from lifecycle_snapshots import export_snapshot
    from state import require_channel
    from transfer_export import build_tree, iter_zip
    block = os.urandom(1024 * 1024)
    size = 200 * len(block)
    digest, stored = store_stream(block for _ in range(200))
    assert stored == size
    made = import_cell(client, client.channel_id, [{'name': 'huge.bin', 'blob_hash': digest}], name='Huge').json()
    snapshot = export_snapshot(require_channel(client.channel_id), made['cell_ids'])
    tree = build_tree(snapshot, 'cell', cell_id=made['cell_ids'][0])
    sink = os.path.join('.', 'huge-export.zip')
    tracemalloc.start()
    try:
        with open(sink, 'wb') as out:
            for piece in iter_zip(tree, snapshot):
                out.write(piece)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16 * 1024 * 1024, peak
    with zipfile.ZipFile(sink) as archive:
        info = archive.getinfo('Huge/huge.bin')
        assert info.file_size == size
        check = hashlib.sha256()
        with archive.open(info) as body:
            while chunk := body.read(1024 * 1024):
                check.update(chunk)
        assert check.hexdigest() == digest
    assert _wait_unpinned() == set()


# ---------------------------------------------------------------------------
# The new-channel scopes: packages, main and tag
# ---------------------------------------------------------------------------

@pytest.fixture
def packaged(client, board):
    """The board plus the renderer cell ``Doc`` on Capabilities, which is
    open on the board: the packages folder is the same wherever the stack
    sits."""
    from cell_placement import capabilities_stack
    from state import require_channel, state_lock
    made = import_cell(client, client.channel_id, [{'name': 'doc.renderer', 'content': 'id: doc'}],
                       name='Doc', stack='Views').json()
    with state_lock:
        ch = require_channel(client.channel_id)
        views = next(s for s in ch.main_stacks if str(s.id) == made['stack_ids'][0])
        ch.main_stacks.remove(views)
        stack = capabilities_stack(ch)
        stack.cells.extend(views.cells)
        ch.nav_stacks.remove(stack)
        ch.main_stacks.append(stack)
    return {**board, 'doc': made['cell_ids'][0]}


def _tag(client, cell_id, tags):
    response = client.patch(f'/api/channels/{client.channel_id}/cells/{cell_id}/meta', json={'tags': tags})
    assert response.status_code == 200, response.text


def _remove(client, cell_id, **body):
    response = client.post(f'/api/channels/{client.channel_id}/cells/{cell_id}/remove', json=body or None)
    assert response.status_code == 200, response.text


def test_manifest_packages_scope_is_the_capabilities_folder(client, packaged):
    manifest = _manifest(client, scope='packages')
    assert manifest['root'] == 'My_ Channel'
    assert _paths(manifest) == {'My_ Channel/Capabilities/Tool/tool.generator',
                                'My_ Channel/Capabilities/Doc/doc.renderer'}
    assert manifest['dirs'] == []


def test_a_cell_moved_off_capabilities_leaves_the_packages_scope(client, packaged):
    from cell_placement import capabilities_stack
    from models import Stack
    from state import require_channel, state_lock
    with state_lock:
        ch = require_channel(client.channel_id)
        doc = capabilities_stack(ch).cells.pop()
        ch.main_stacks.append(Stack(name='Off', cells=[doc]))
    assert _paths(_manifest(client, scope='packages')) == {'My_ Channel/Capabilities/Tool/tool.generator'}


def test_new_channel_scopes_download_under_distinct_names(client, packaged):
    """The zip keeps the scope's root folder, but its file name says what
    was exported, so the packages, main and tag zips of one channel never
    overwrite each other in a downloads folder."""
    _tag(client, packaged['notes'], ['keep'])
    names = {}
    for scope, extra in (('packages', {}), ('main', {}), ('tag', {'tag': 'keep'}), ('all', {})):
        response = client.get(f'/api/channels/{client.channel_id}/export.zip', params={'scope': scope, **extra})
        assert response.status_code == 200, response.text
        names[scope] = response.headers['content-disposition']
    assert names == {
        'packages': "attachment; filename=\"My_ Channel packages.zip\"; filename*=UTF-8''My_%20Channel%20packages.zip",
        'main': "attachment; filename=\"My_ Channel main cells.zip\"; filename*=UTF-8''My_%20Channel%20main%20cells.zip",
        'tag': "attachment; filename=\"My_ Channel keep cells.zip\"; filename*=UTF-8''My_%20Channel%20keep%20cells.zip",
        'all': "attachment; filename=\"My_ Channel.zip\"; filename*=UTF-8''My_%20Channel.zip",
    }
    assert _wait_unpinned() == set()


def test_manifest_main_scope_is_the_standard_main_stacks(client, packaged):
    manifest = _manifest(client, scope='main')
    assert manifest['root'] == 'My_ Channel'
    paths = _paths(manifest)
    assert {'My_ Channel/Docs/Notes/a.txt', 'My_ Channel/Docs/Notes/sub/b.md',
            'My_ Channel/A_B/CON_/c.txt'} <= paths
    assert {p.split('/')[1] for p in paths} == {'Docs', 'A_B'}
    assert manifest['dirs'] == ['My_ Channel/Docs/Notes (2)', 'My_ Channel/Docs/Notes/empty']


def test_tag_scope_holds_live_and_removed_cells_casefolded(client, board):
    """The tag's set is what the tag list counts: live standard-stack cells
    and removed cells carrying it in any case, flat under the tag. A removed
    cell's files are its retained tip, run folders left out."""
    _tag(client, board['notes'], ['Keep', 'other'])
    _tag(client, board['twin'], ['nope'])
    _host_put(client.channel_id, board['con'], '_runs/r-9/log.txt', 'run log')
    _remove(client, board['con'], add_tags=['KEEP'])
    counted = {t['tag'].casefold(): t for t in client.get(f'/api/channels/{client.channel_id}/tags').json()}
    assert (counted['keep']['live'], counted['keep']['removed']) == (1, 1)
    manifest = _manifest(client, scope='tag', tag='keep')
    assert manifest['root'] == 'keep'
    assert _paths(manifest) == {'keep/Notes/a.txt', 'keep/Notes/sub/b.md', 'keep/Notes/img.png',
                                'keep/Notes/Read.md', 'keep/Notes/read (2).md', 'keep/Notes/odd_name_.txt',
                                'keep/CON_/c.txt'}
    response = client.get(f'/api/channels/{client.channel_id}/export.zip', params={'scope': 'tag', 'tag': ' keep '})
    assert response.status_code == 200, response.text
    assert response.headers['content-disposition'].startswith('attachment; filename="My_ Channel keep cells.zip"')
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.read('keep/CON_/c.txt') == b'gamma'
    assert archive.read('keep/Notes/img.png') == PNG
    assert _wait_unpinned() == set()


def test_freeze_tag_cells_pins_removed_bodies_until_release(client, board):
    from cas import pinned_hashes
    from lifecycle_snapshots import freeze_tag_cells
    from state import require_channel
    _remove(client, board['con'], add_tags=['Archive'])
    with freeze_tag_cells(require_channel(client.channel_id), 'archive') as snapshot:
        assert snapshot.files_by_cell == {}
        [item] = snapshot.removed
        assert (str(item.cell.id), item.cell.name, item.cell.tags, item.stack_name) == (
            board['con'], 'CON', ['Archive'], 'A:B')
        assert item.cell.files == []
        assert [f.name for f in item.files] == ['c.txt']
        assert hashlib.sha256(b'gamma').hexdigest() in pinned_hashes()
    assert pinned_hashes() == set()


def test_tag_scope_arguments_are_checked(client, board):
    base = f'/api/channels/{client.channel_id}/export/manifest'
    assert client.get(base, params={'scope': 'tag'}).status_code == 400
    _tag(client, board['notes'], ['Keep'])
    assert client.get(base, params={'scope': 'tag', 'tag': 'missing'}).status_code == 404
    assert client.get(base, params={'scope': 'tag', 'tag': '\x01'}).status_code == 404
    zipped = client.get(f'/api/channels/{client.channel_id}/export.zip', params={'scope': 'tag', 'tag': 'missing'})
    assert zipped.status_code == 404
    from cas import pinned_hashes
    assert pinned_hashes() == set()


def test_tag_set_changing_during_the_freeze_answers_retry(client, board, monkeypatch):
    """A cell tagged between the selection and the freeze is not left out
    silently: the frozen set is re-checked under the locks, 409, retry."""
    import cell_commits
    from state import require_channel, state_lock
    _tag(client, board['notes'], ['Keep'])
    locks = cell_commits.cell_commit_locks

    def tag_then_lock(cell_ids):
        with state_lock:
            ch = require_channel(client.channel_id)
            twin = next(c for s in ch.main_stacks for c in s.cells if str(c.id) == board['twin'])
            twin.tags = ['keep']
        return locks(cell_ids)

    monkeypatch.setattr(cell_commits, 'cell_commit_locks', tag_then_lock)
    response = client.get(f'/api/channels/{client.channel_id}/export/manifest', params={'scope': 'tag', 'tag': 'keep'})
    assert response.status_code == 409, response.text
    assert _wait_unpinned() == set()


def test_tag_freeze_waits_for_a_removed_cells_lock(client, board):
    """The removed cells' commit locks are held while their tip is read, so
    a restore (which takes that lock) cannot interleave with the freeze."""
    from cell_commits import cell_commit_lock
    from lifecycle_snapshots import freeze_tag_cells
    from state import require_channel
    _remove(client, board['con'], add_tags=['Archive'])
    channel = require_channel(client.channel_id)
    held, done, result = threading.Event(), threading.Event(), {}

    def hold():
        with cell_commit_lock(board['con']):
            held.set()
            done.wait(10)

    def freeze():
        with freeze_tag_cells(channel, 'Archive') as snapshot:
            result['removed'] = [str(item.cell.id) for item in snapshot.removed]

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        freezer = threading.Thread(target=freeze)
        freezer.start()
        freezer.join(0.5)
        assert freezer.is_alive(), 'the tag freeze should wait for the removed cell'
    finally:
        done.set()
        holder.join()
    freezer.join(5)
    assert result['removed'] == [board['con']]


# ---------------------------------------------------------------------------
# GET /api/channels/{channel}/outline
# ---------------------------------------------------------------------------

def test_outline_lists_package_cells_and_standard_main_stacks(client, packaged):
    response = client.get(f'/api/channels/{client.channel_id}/outline')
    assert response.status_code == 200, response.text
    outline = response.json()
    assert outline['capabilities_cells'] == [
        {'id': packaged['tool'], 'name': 'Tool'},
        {'id': packaged['doc'], 'name': 'Doc'}]
    assert [(s['name'], [c['name'] for c in s['cells']]) for s in outline['main_stacks']] == [
        ('Docs', ['Notes', 'Notes']), ('A:B', ['CON'])]
    docs = outline['main_stacks'][0]
    assert docs['id'] == packaged['docs']
    assert [c['id'] for c in docs['cells']] == [packaged['notes'], packaged['twin']]
