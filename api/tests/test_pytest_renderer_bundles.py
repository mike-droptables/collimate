"""Renderer source freshness and bundle-route regressions."""
import hashlib
import json
import uuid
from pathlib import Path

import pytest
import yaml

from conftest import install_test_renderer
from cell_placement import capabilities_stack, capabilities_stack_cells


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from genesis import ensure_capabilities_stack
    from models import Channel
    from state import state
    from database import get_db_connection
    with TestClient(api_app) as http:
        channel = Channel(name='Starting')
        ensure_capabilities_stack(channel)
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        yield http


def _accept_files(rid, files):
    from persistence import mutate_files, snapshot_files
    from state import get_active_channel
    from database import get_db_connection
    from cas import store_bytes
    cell = _renderer_cell(rid)
    channel = str(get_active_channel().id)
    with get_db_connection(channel) as conn:
        revisions = {f['path']: f['file_revision'] for f in snapshot_files(conn, str(cell.id), include_deleted=True)}
    mutations = []
    for path, body in files.items():
        mutation = dict(path=path, kind='delete' if body is None else 'put',
                        base_revision=revisions.get(path, 0), writer_id='renderer-fixture', edit_seq=revisions.get(path, 0) + 1)
        if isinstance(body, bytes):
            mutation.update(blob_hash=store_bytes(body), encoding='binary', language='binary')
        elif body is not None:
            mutation['content_text'] = body
        mutations.append(mutation)
    receipt = mutate_files(channel, str(cell.id), dict(operation_id=str(uuid.uuid4()), mutations=mutations), actor='host')
    assert receipt['outcome'] == 'accepted'
    return receipt


def test_renderer_source_uses_matching_bundle_and_never_unbuilt_sibling_js__F0251(client):
    from state import find_active_channel

    renderer_id = install_test_renderer(main_content="// newer client source\n")
    channel = find_active_channel()
    cell = next(cell for cell in capabilities_stack_cells(channel)
                if any(file.name == f"{renderer_id}/main.tsx" for file in cell.files))
    _source = next(file for file in cell.files if file.name.endswith("/main.tsx"))
    _accept_files(renderer_id, {f"{renderer_id}/main.js": "// unbuilt sibling output\n"})
    # Sibling JS is source input; only the separately published matching bundle executes.
    current = next(row for row in client.get('/api/renderers').json() if row['id'] == renderer_id)
    _publish(current['content_hash'], b'export default () => "current bundle";')
    response = client.get(f"/api/renderers/{renderer_id}/source")
    assert response.status_code == 200
    assert response.text == 'export default () => "current bundle";'
    assert response.headers["X-Entrypoint"] == "module.js"


def test_renderer_source_never_serves_unbuilt_source__F0251(client):
    renderer_id = install_test_renderer()
    response = client.get(f"/api/renderers/{renderer_id}/source")
    assert response.status_code == 404


def test_renderer_source_rejects_stale_version_url__F0295(client):
    renderer_id = install_test_renderer()
    response = client.get(f"/api/renderers/{renderer_id}/source?v={'0' * 64}")
    assert response.status_code == 404


def test_renderer_discovery_follows_accepted_manifest_after_cache_publication_failure(client, monkeypatch):
    import persistence
    from state import get_active_channel

    old_id = install_test_renderer(renderer_id='old-renderer')
    cell = _renderer_cell(old_id)
    channel = str(get_active_channel().id)
    manifest_path = f'{old_id}/view.renderer'
    _descriptor, raw = persistence.read_file(channel, str(cell.id), manifest_path)
    manifest = yaml.safe_load(raw)
    manifest['id'] = 'new-renderer'

    def failed_cache(*_args, **_kwargs):
        raise OSError('injected derived cache publication failure')

    monkeypatch.setattr(persistence, 'publish_files', failed_cache)
    receipt = _accept_files(old_id, {manifest_path: yaml.safe_dump(manifest)})
    assert receipt['outcome'] == 'accepted'
    assert yaml.safe_load(persistence.read_file(channel, str(cell.id), manifest_path)[1])['id'] == 'new-renderer'
    assert any(file.name == manifest_path and yaml.safe_load(file.content)['id'] == old_id
               for file in cell.files), 'The derived cell cache must remain stale for this regression'

    listed = client.get('/api/renderers')
    assert listed.status_code == 200
    assert [item['id'] for item in listed.json() if not item['builtin']] == ['new-renderer']
    current = listed.json()[0]
    assert current['cell_id'] == str(cell.id)
    assert client.get(f"/api/renderers/{old_id}/package", params={'v': current['content_hash']}).status_code == 404
    package = client.get('/api/renderers/new-renderer/package', params={'v': current['content_hash']})
    assert package.status_code == 200
    assert package.json()['source_hash'] == current['content_hash']


def _publish(source, body):
    """Store ``body`` as the server bundle of ``source``; answers its digest."""
    from renderer_bundles import publish_server_bundle
    return publish_server_bundle(source, body)


def _origin(source):
    """Who compiled the stored bundle of ``source`` (None: none stored)."""
    import database
    conn = database.get_global_db_connection()
    try:
        row = conn.execute('SELECT origin FROM renderer_bundles WHERE source_hash=?', (source,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def test_renderer_bundle_freshness_caching_and_sibling_changes__F0251_F0295(client):
    from state import find_active_channel
    rid = install_test_renderer()
    _cell = next(c for c in capabilities_stack_cells(find_active_channel())
                 if any(f.name == f'{rid}/main.tsx' for f in c.files))
    current = next(r for r in client.get('/api/renderers').json() if r['id'] == rid)['content_hash']
    digest = hashlib.sha256(b'export default () => null;').hexdigest()
    assert _publish(current, b'export default () => null;') == digest
    assert _publish(current, b'export default () => null;') == digest
    url = f'/api/renderers/{rid}/source'
    built = client.get(url, params={'v': current})
    assert built.status_code == 200
    assert built.headers['Cache-Control'] == 'private, max-age=31536000, immutable'
    assert built.headers['ETag'] == f'"{current}"'
    assert client.get(url, headers={'If-None-Match': built.headers['ETag']}).status_code == 304
    assert client.get(url).headers['Cache-Control'] == 'no-cache'
    # A sibling edit with no timestamp relation invalidates the whole package.
    _accept_files(rid, {f'{rid}/helper.ts': 'export const value = 1;'})
    assert client.get(url, params={'v': current}).status_code == 404
    assert client.get(url).status_code == 404


def test_server_bundle_preserves_bytes_and_survives_reopen__F1868_F0251(fresh_cwd):
    from renderer_bundles import get_bundle
    import database
    body = b'// compiler output\r\nexport default () => null;\n'
    source = 'b' * 64
    digest = hashlib.sha256(body).hexdigest()
    assert _publish(source, body) == digest
    database._initialised_dbs.clear()
    assert get_bundle(source) == body
    assert _publish(source, body) == digest
    conn = database.get_global_db_connection()
    assert [tuple(row) for row in conn.execute('SELECT * FROM renderer_bundles')] == [(source, digest, 'server')]
    conn.close()
    from cas import blob_path
    Path(blob_path(digest)).write_bytes(b'damaged')
    assert get_bundle(source) is None


def test_a_server_bundle_replaces_a_client_upload_and_no_upload_replaces_it(client):
    from renderer_bundles import get_bundle
    rid = install_test_renderer()
    source = _renderer_hash(client, rid)
    endpoint = f'/api/renderers/{rid}/bundle'
    uploaded = client.put(endpoint, json={'source_hash': source, 'code': 'export default () => "client";'})
    assert uploaded.status_code == 200 and _origin(source) == 'client'
    server = b'export default () => "server";'
    digest = _publish(source, server)
    assert (get_bundle(source), _origin(source)) == (server, 'server')
    # An upload for a server bundle changes nothing and answers its digest.
    for code in ('export default () => "client";', 'export default () => "server";', 'other'):
        answer = client.put(endpoint, json={'source_hash': source, 'code': code})
        assert answer.status_code == 200, answer.text
        assert answer.json() == {'source_hash': source, 'bundle_hash': digest}
    assert client.get(f'/api/renderers/{rid}/source', params={'v': source}).content == server
    assert _origin(source) == 'server'


def test_admission_serves_the_node_build_bytes_of_raw_source_with_no_upload__F1868_F0251_F0295(api_app, tmp_path):
    """A channel created from a folder suggestion admits its renderer and
    the server compiles it: the source route serves that bundle straight
    away, byte for byte what the Node build compiles for the same raw source
    (CRLF, a non-ASCII sibling, a binary member), and a later start still
    serves it."""
    import subprocess
    import sys
    from fastapi.testclient import TestClient
    from renderer_bundles import get_bundle

    renderers = tmp_path / 'build-assets' / 'renderers'
    package = renderers / 'Text' / 'arbitrary-folder'
    package.mkdir(parents=True)
    files = {
        'choice.renderer': b'id: raw-source\r\nname: Raw source\r\nsummary: Raw byte parity fixture\r\nentrypoint: main.tsx\r\nembed: native\r\nextensions: [.raw]\r\n',
        'main.tsx': 'import { defineRenderer } from "/api/sdk/react.js";\r\nimport { value } from "./π-helper.ts";\r\nconst Component = () => value;\r\nexport default defineRenderer({ component: Component });\r\n'.encode(),
        'π-helper.ts': b'export const value = "bundled sibling";\r\n',
        'asset.bin': bytes([0, 255, 128, 1]),
    }
    for name, body in files.items():
        (package / name).write_bytes(body)
    artifacts = tmp_path / 'node-build'
    builtin = tmp_path / 'no-builtin'  # no built-in packages: this index holds the one renderer
    builtin.mkdir()
    subprocess.run([sys.executable, '-m', 'collimate._host.renderer_bundle', '--build', str(renderers),
                    '--builtin', str(builtin), '--out', str(artifacts)], check=True, capture_output=True, timeout=30)
    index = json.loads((artifacts / 'index.json').read_text())
    assert len(index['entries']) == 1
    expected = index['entries'][0]
    suggestion = renderers.parent / 'suggested' / 'parity.json'
    suggestion.parent.mkdir()
    suggestion.write_text(json.dumps({'name': 'Parity', 'description': '', 'generators': [],
                                      'renderers': [['Text', 'renderers/Text']], 'tutorial': [], 'quick_fields': {}}))
    with TestClient(api_app) as browser:
        assert browser.put('/api/suggested/source', json={'path': str(renderers.parent)}).status_code == 200
        created = browser.post('/api/channels', params={'name': 'Build parity'},
                               json={'suggested': {'id': 'parity', 'tutorial': False}})
        assert created.status_code == 200, created.text
        catalog = browser.get('/api/renderers').json()
        entry = next(item for item in catalog if item['id'] == 'raw-source')
        assert entry['content_hash'] == expected['source_hash']
        result = browser.get('/api/renderers/raw-source/source', params={'v': entry['content_hash']})
        assert result.status_code == 200
        assert result.content == (artifacts / expected['file']).read_bytes()
        assert hashlib.sha256(result.content).hexdigest() == expected['bundle_hash']
        assert b'./' not in result.content
        assert b'bundled sibling' in result.content
        assert _origin(expected['source_hash']) == 'server'
    # The stored bundle outlives the process that compiled it.
    with TestClient(api_app) as browser:
        assert browser.get('/api/renderers/raw-source/source').content == result.content
    assert get_bundle(expected['source_hash']) == result.content


CONTENT = Path(__file__).resolve().parents[2] / 'content'


def _standard_channel(client, name):
    """A channel created from the shipped Standard suggestion (the content
    tree as a folder source); answers its id. It becomes the active one."""
    from suggested_trees import use_source
    use_source(client, CONTENT)
    created = client.post('/api/channels', params={'name': name},
                          json={'suggested': {'id': 'standard', 'tutorial': False, 'runners': []}})
    assert created.status_code == 200, created.text
    return created.json()['id']


def _shipped_bundle(folder):
    """What ``collimate._host.renderer_bundle`` compiles for the shipped renderer
    package ``content/renderers/<folder>``, as the build reads it."""
    from collimate._host.renderer_bundle import bundle_renderer
    root = CONTENT / 'renderers' / folder
    files = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*')
             if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo')}
    definition = next(path for path in files if path.endswith('.renderer'))
    return bundle_renderer(files, entrypoint=yaml.safe_load(files[definition])['entrypoint'])


def test_a_shipped_renderer_admitted_from_the_standard_suggestion_is_served_with_no_upload(api_app):
    """Admission compiles a shipped renderer and stores the result under
    the source hash the source route computes for the seeded cell: served
    straight away, the bundle ``collimate._host.renderer_bundle`` compiles."""
    from fastapi.testclient import TestClient
    expected = _shipped_bundle('visual/mermaid')
    with TestClient(api_app) as client:
        _standard_channel(client, 'Seeded')
        entry = next(row for row in client.get('/api/renderers').json() if row['id'] == 'mermaid')
        assert entry['builtin'] is False and entry['content_hash'] == expected.source_hash
        served = client.get('/api/renderers/mermaid/source', params={'v': entry['content_hash']})
        assert served.status_code == 200, served.text
        assert hashlib.sha256(served.content).hexdigest() == expected.bundle_hash
        assert served.content == expected.code.encode('utf-8')
        assert _origin(expected.source_hash) == 'server'


def test_a_shipped_renderer_zip_imported_into_another_channel_gets_the_seeded_bundle_hash(api_app):
    """The Phase 2 gate: a shipped renderer's cell, exported as a zip and
    imported into a new channel, is admitted like the seeded one and served
    the same bundle, compiled by that import's own admission."""
    import database
    from fastapi.testclient import TestClient
    with TestClient(api_app) as client:
        seeded_id = _standard_channel(client, 'Seeded')
        seeded = next(row for row in client.get('/api/renderers').json() if row['id'] == 'mermaid')
        served = client.get('/api/renderers/mermaid/source', params={'v': seeded['content_hash']})
        assert served.status_code == 200, served.text
        # Forget every stored bundle: only the import can serve the copy.
        conn = database.get_global_db_connection()
        try:
            with conn:
                conn.execute('DELETE FROM renderer_bundles')
        finally:
            conn.close()
        exported = client.get(f'/api/channels/{seeded_id}/export.zip',
                              params={'scope': 'cell', 'cell_id': seeded['cell_id']})
        assert exported.status_code == 200, exported.text
        expanded = client.post('/api/import/zip', content=exported.content)
        assert expanded.status_code == 200, expanded.text
        files = [{'path': entry['path'].split('/', 1)[1], 'blob_hash': entry['blob_hash']}
                 for entry in expanded.json()['entries']]
        assert any(file['path'] == 'mermaid/mermaid.renderer' for file in files)
        copy = client.post('/api/channels', params={'name': 'Copy'},
                           json={'config': {'packages': {'cells': [{'name': 'Visual', 'files': files}]}}})
        assert copy.status_code == 200, copy.text
        assert copy.json()['package_skips'] == []
        imported = next(row for row in client.get('/api/renderers').json() if row['id'] == 'mermaid')
        assert imported['cell_id'] != seeded['cell_id']
        assert imported['content_hash'] == seeded['content_hash']
        again = client.get('/api/renderers/mermaid/source', params={'v': imported['content_hash']})
        assert again.status_code == 200, again.text
        assert hashlib.sha256(again.content).hexdigest() == hashlib.sha256(served.content).hexdigest()
        assert _origin(imported['content_hash']) == 'server'


def test_an_import_refused_at_admission_stores_no_bundle(client):
    """A renderer admitted beside a package that is then refused is never
    published, and leaves no bundle; admitted alone, it is served."""
    from collimate._host.renderer_bundle import bundle_renderer
    from import_cells import upload
    from renderer_bundles import get_bundle
    manifest = b'id: kept-view\nname: Kept\nsummary: Kept\nentrypoint: main.tsx\nembed: native\nextensions: [.kept]\n'
    main = (b"import { defineRenderer } from '/api/sdk/react.js';\nconst Component = () => 'kept';\n"
            b'export default defineRenderer({ component: Component });\n')
    expected = bundle_renderer({'view.renderer': manifest, 'main.tsx': main}, entrypoint='main.tsx')
    kept = {'name': 'Kept', 'files': [
        {'path': 'kept/view.renderer', 'blob_hash': upload(client, manifest)['blob_hash']},
        {'path': 'kept/main.tsx', 'blob_hash': upload(client, main)['blob_hash']}]}
    broken = {'name': 'Broken', 'files': [
        {'path': 'broken/view.renderer', 'blob_hash': upload(client, (
            b'id: broken\nname: Broken\nentrypoint: main.tsx\nembed: native\nextensions: [.broken]\n'))['blob_hash']},
        {'path': 'broken/main.tsx', 'blob_hash': upload(client, b'export default {{{\n')['blob_hash']}]}
    refused = client.post('/api/channels', params={'name': 'Refused'},
                          json={'config': {'packages': {'cells': [kept, broken]}}})
    assert refused.status_code == 400, refused.text
    assert get_bundle(expected.source_hash) is None
    created = client.post('/api/channels', params={'name': 'Kept'}, json={'config': {'packages': {'cells': [kept]}}})
    assert created.status_code == 200, created.text
    assert _renderer_hash(client, 'kept-view') == expected.source_hash
    served = client.get('/api/renderers/kept-view/source', params={'v': expected.source_hash})
    assert served.status_code == 200 and served.content == expected.code.encode('utf-8')
    assert _origin(expected.source_hash) == 'server'


def test_add_to_stores_the_admitted_renderer_bundle(client):
    """Add to (a reviewed package from any cell) is admission too: the
    copied renderer is served its server bundle with no upload."""
    from collimate._host.renderer_bundle import bundle_renderer
    from import_cells import import_cell
    from state import get_active_channel
    manifest = 'id: added-view\nname: Added\nsummary: Added\nentrypoint: main.tsx\nembed: native\nextensions: [.added]\n'
    main = ("import { defineRenderer } from '/api/sdk/react.js';\nconst Component = () => 'added';\n"
            'export default defineRenderer({ component: Component });\n')
    expected = bundle_renderer({'view.renderer': manifest.encode(), 'main.tsx': main.encode()}, entrypoint='main.tsx')
    channel = str(get_active_channel().id)
    made = import_cell(client, channel, [{'name': 'added/view.renderer', 'content': manifest},
                                         {'name': 'added/main.tsx', 'content': main}], name='Draft')
    assert made.status_code == 200, made.text
    request = {'source_cell_id': made.json()['cell_ids'][0], 'definition_path': 'added/view.renderer',
               'target_cell_id': None}
    preview = client.post(f'/api/channels/{channel}/packages/preview', json=request)
    assert preview.status_code == 200, preview.text
    from renderer_bundles import get_bundle
    assert get_bundle(expected.source_hash) is None  # a preview stores nothing
    added = client.post(f'/api/channels/{channel}/packages/add', json={
        **request, 'expected_content_hash': preview.json()['content_hash'],
        'expected_target_revision': preview.json()['target_revision']})
    assert added.status_code == 200, added.text
    assert _renderer_hash(client, 'added-view') == expected.source_hash
    served = client.get('/api/renderers/added-view/source', params={'v': expected.source_hash})
    assert served.status_code == 200 and served.content == expected.code.encode('utf-8')
    assert _origin(expected.source_hash) == 'server'


def _renderer_cell(rid):
    from state import find_active_channel
    return next(c for c in capabilities_stack_cells(find_active_channel())
                if any(f.name == f'{rid}/main.tsx' for f in c.files))


def _renderer_hash(client, rid):
    return next(r for r in client.get('/api/renderers').json() if r['id'] == rid)['content_hash']


def test_unbuilt_package_snapshot_preserves_exact_bytes_and_boundary__F0251_F1868_P0F21(client):
    import base64
    rid = install_test_renderer(main_content='// exact\r\nexport default () => null;\r\n')
    _cell = _renderer_cell(rid)
    _accept_files(rid, {
        f'{rid}/π.ts': 'export const value="π";\r\n',
        f'{rid}/asset.bin': b'\x00\xff\x80',
        f'{rid}/_runs/private.txt': 'NEVER_INCLUDE_RUN',
        'adjacent/private.ts': 'NEVER_INCLUDE_ADJACENT',
    })
    source = _renderer_hash(client, rid)
    response = client.get(f'/api/renderers/{rid}/package', params={'v': source})
    assert response.status_code == 200
    data = response.json()
    assert data['source_hash'] == source and data['entrypoint'] == 'main.tsx'
    assert response.headers['Cache-Control'] == 'no-store'
    raw = {path: base64.b64decode(body, validate=True) for path, body in data['files'].items()}
    assert raw['main.tsx'] == b'// exact\r\nexport default () => null;\r\n'
    assert raw['π.ts'] == 'export const value="π";\r\n'.encode()
    assert raw['asset.bin'] == b'\x00\xff\x80'
    assert not any('_runs' in path.split('/') or path.startswith('adjacent/') for path in raw)
    from collimate._host.renderer_bundle import renderer_source_hash
    assert renderer_source_hash({p: hashlib.sha256(b).hexdigest() for p, b in raw.items()},
                                entrypoint=data['entrypoint']) == source


def test_client_upload_fills_unbuilt_cache_and_retains_first_identical_version__F0251_F0295(client):
    from renderer_bundles import get_bundle
    rid = install_test_renderer()
    source = _renderer_hash(client, rid)
    url = f'/api/renderers/{rid}/source'
    assert client.get(url, params={'v': source}).status_code == 404
    body = 'export default () => "user compiled bundle";\r\n'
    endpoint = f'/api/renderers/{rid}/bundle'
    answer = client.put(endpoint, json={'source_hash': source, 'code': body})
    assert answer.status_code == 200
    assert answer.json() == {'source_hash': source, 'bundle_hash': hashlib.sha256(body.encode()).hexdigest()}
    assert client.put(endpoint, json={'source_hash': source, 'code': body}).json() == answer.json()
    assert client.put(endpoint, json={'source_hash': source, 'code': 'different'}).status_code == 409
    assert client.put(endpoint, content=json.dumps({'source_hash': source, 'code': '\ud800'}),
                      headers={'Content-Type': 'application/json'}).status_code == 422
    built = client.get(url, params={'v': source})
    assert built.content == body.encode()
    assert built.headers['Cache-Control'] == 'private, max-age=31536000, immutable'
    assert built.headers['ETag'] == f'"{source}"' and built.headers['X-Entrypoint'] == 'module.js'
    import database
    database._initialised_dbs.clear()
    assert get_bundle(source) == body.encode()


def test_client_snapshot_and_upload_refuse_stale_source_version__F0251_F0295(client):
    from renderer_bundles import get_bundle
    rid = install_test_renderer()
    previous = _renderer_hash(client, rid)
    _accept_files(rid, {f'{rid}/helper.ts': 'export const n=1;'})
    assert _renderer_hash(client, rid) != previous
    assert client.get(f'/api/renderers/{rid}/package', params={'v': previous}).status_code == 404
    response = client.put(f'/api/renderers/{rid}/bundle', json={'source_hash': previous, 'code': 'old'})
    assert response.status_code == 409
    assert get_bundle(previous) is None


def test_package_and_upload_keep_existing_auth_origin_and_enabled_boundary__F0251_P0F21(client):
    from auth_middleware import auth
    rid = install_test_renderer()
    source = _renderer_hash(client, rid)
    package = f'/api/renderers/{rid}/package?v={source}'
    bundle = f'/api/renderers/{rid}/bundle'
    payload = {'source_hash': source, 'code': 'export default {};'}
    auth.configure('synthetic-step2-token')
    try:
        assert client.get(package).status_code == 401
        assert client.put(bundle, json=payload).status_code == 401
        headers = {'Authorization': 'Bearer synthetic-step2-token'}
        assert client.get(package, headers=headers).status_code == 200
        # In tokened mode the bearer is the authority for non-opaque origins.
        assert client.put(bundle, json=payload, headers=headers | {'Origin': 'https://foreign.invalid'}).status_code == 200
        assert client.put(bundle, json=payload, headers=headers | {'Origin': 'null'}).status_code == 403
    finally:
        auth.configure(None)
    assert client.put(bundle, json=payload, headers={'Origin': 'https://foreign.invalid'}).status_code == 403
    assert client.put(f'/api/renderer_availability/{rid}', json={'enabled': False}).status_code == 200
    assert client.get(package).status_code == 404
    assert client.put(bundle, json=payload).status_code == 404


def test_upload_rechecks_source_after_staging_before_cache_publication__F0251_F0295(client, monkeypatch):
    import cas
    import database
    from renderer_bundles import get_bundle
    rid = install_test_renderer()
    source = _renderer_hash(client, rid)
    original = cas.store_bytes
    body = 'export default () => "raced";'

    def mutate_after_store(raw):
        digest = original(raw)
        if raw == body.encode():
            _accept_files(rid, {f'{rid}/new.ts': 'changed'})
        return digest

    monkeypatch.setattr(cas, 'store_bytes', mutate_after_store)
    response = client.put(f'/api/renderers/{rid}/bundle', json={'source_hash': source, 'code': body})
    assert response.status_code == 409
    assert get_bundle(source) is None
    conn = database.get_global_db_connection()
    assert conn.execute('SELECT COUNT(*) FROM renderer_bundles WHERE source_hash=?', (source,)).fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize('late_change', ['source-edit', 'disable', 'placement'])
def test_upload_refuses_change_injected_before_cache_commit_on_final_recheck__F0251_F0295(client, monkeypatch, late_change):
    import cas
    from channel_config import set_renderer_availability
    from renderer_bundles import get_bundle
    from state import get_active_channel, state_lock

    rid = install_test_renderer()
    cell = _renderer_cell(rid)
    channel = get_active_channel()
    source = _renderer_hash(client, rid)
    code = f'export default () => "late {late_change}";'
    digest = hashlib.sha256(code.encode()).hexdigest()
    original_retain = cas.retain_blob
    changed = False

    def change_after_retain(value):
        nonlocal changed
        result = original_retain(value)
        if value == digest and not changed:
            changed = True
            if late_change == 'source-edit':
                _accept_files(rid, {f'{rid}/late.ts': 'export const changed = true;'})
            elif late_change == 'disable':
                set_renderer_availability(str(channel.id), rid, False)
            else:
                with state_lock:
                    capabilities_stack(channel).cells.remove(cell)
        return result

    monkeypatch.setattr(cas, 'retain_blob', change_after_retain)
    response = client.put(f'/api/renderers/{rid}/bundle', json={'source_hash': source, 'code': code})
    assert changed and response.status_code == 409
    # The old hash may retain derived bundle bytes. It cannot be served from a
    # changed source, disabled renderer, or removed placement.
    assert get_bundle(source) == code.encode()
    assert client.get(f'/api/renderers/{rid}/source', params={'v': source}).status_code == 404


def test_uploaded_bundle_is_a_durable_aged_gc_root__F0251_F0295(client):
    import os
    from cas import blob_path
    from renderer_bundles import get_bundle
    import cas_gc
    rid = install_test_renderer()
    source = _renderer_hash(client, rid)
    code = 'export default () => "cached user module";'
    response = client.put(f'/api/renderers/{rid}/bundle', json={'source_hash': source, 'code': code})
    assert response.status_code == 200
    digest = response.json()['bundle_hash']
    os.utime(blob_path(digest), (0, 0))
    result = cas_gc.run_gc()
    assert result.get('aborted') is not True
    assert Path(blob_path(digest)).is_file()
    assert get_bundle(source) == code.encode()


def test_package_uses_accepted_source_despite_stale_history_and_derived_cache__F0251_F0295(client):
    import base64
    from state import get_active_channel
    rid = install_test_renderer(main_content='// historical\r\n')
    cell = _renderer_cell(rid)
    channel = str(get_active_channel().id)
    assert client.post(f'/api/channels/{channel}/cells/{cell.id}/checkpoint').status_code == 200
    historical = _renderer_hash(client, rid)
    assert base64.b64decode(client.get(f'/api/renderers/{rid}/package', params={'v': historical}).json()['files']['main.tsx']) == b'// historical\r\n'
    current_bytes = '// accepted current π\r\n'.encode()
    _accept_files(rid, {f'{rid}/main.tsx': current_bytes.decode()})
    current = _renderer_hash(client, rid)
    assert current != historical
    # Derived memory is not a second current-body authority.
    next(f for f in cell.files if f.name == f'{rid}/main.tsx').content = ''
    assert client.get(f'/api/renderers/{rid}/package', params={'v': historical}).status_code == 404
    response = client.get(f'/api/renderers/{rid}/package', params={'v': current})
    assert response.status_code == 200, response.text
    assert base64.b64decode(response.json()['files']['main.tsx']) == current_bytes
    assert _renderer_hash(client, rid) == current
    assert client.put(f'/api/renderers/{rid}/bundle', json={'source_hash': current, 'code': 'export default {};'}).status_code == 200
    assert client.get(f'/api/renderers/{rid}/source', params={'v': current}).text == 'export default {};'
    _accept_files(rid, {f'{rid}/main.tsx': None})
    assert client.get(f'/api/renderers/{rid}/package', params={'v': current}).status_code == 404


def test_package_refuses_missing_binary_objects_and_invalid_upload_shapes__F0251_P0F21(client):
    rid = install_test_renderer()
    _cell = _renderer_cell(rid)
    _accept_files(rid, {f'{rid}/asset.bin': b'actual binary body'})
    source = _renderer_hash(client, rid)
    from cas import blob_path
    Path(blob_path(hashlib.sha256(b'actual binary body').hexdigest())).unlink()
    assert client.get(f'/api/renderers/{rid}/package', params={'v': source}).status_code == 404
    assert client.put(f'/api/renderers/{rid}/bundle', json={'source_hash': source, 'code': 'bad'}).status_code == 404
    assert client.put(f'/api/renderers/{rid}/bundle', json={'source_hash': '../escape', 'code': 'bad'}).status_code == 422
    assert client.put(f'/api/renderers/{rid}/bundle', json={'source_hash': source, 'code': {}, 'extra': True}).status_code == 422


def test_actual_edited_package_compile_upload_then_current_module_serving__F0251_F0295_F1868(client, tmp_path):
    import base64
    import subprocess
    import sys
    rid = install_test_renderer(main_content="import {value} from './sibling'; export default () => value;\r\n")
    _cell = _renderer_cell(rid)
    _accept_files(rid, {f'{rid}/sibling.ts': 'export const value="edited module π";\r\n'})
    source = _renderer_hash(client, rid)
    assert client.get(f'/api/renderers/{rid}/source', params={'v': source}).status_code == 404
    package = client.get(f'/api/renderers/{rid}/package', params={'v': source})
    assert package.status_code == 200
    root = tmp_path / 'client-renderers'
    folder = root / 'cell' / 'authored'
    folder.mkdir(parents=True)
    for path, value in package.json()['files'].items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(value))
    output = tmp_path / 'client-output'
    builtin = tmp_path / 'no-builtin'  # this index holds the one edited renderer
    builtin.mkdir()
    compiled = subprocess.run([sys.executable, '-m', 'collimate._host.renderer_bundle', '--build', str(root),
                               '--builtin', str(builtin), '--out', str(output)],
                              capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stderr
    [entry] = json.loads((output / 'index.json').read_text())['entries']
    assert entry['source_hash'] == source
    code = (output / entry['file']).read_bytes()
    assert b'./sibling' not in code and b'edited module \\u03C0' in code
    response = client.put(f'/api/renderers/{rid}/bundle', json={'source_hash': source, 'code': code.decode('utf-8')})
    assert response.status_code == 200
    assert response.json()['bundle_hash'] == entry['bundle_hash']
    assert client.get(f'/api/renderers/{rid}/source', params={'v': source}).content == code


def test_upload_publishes_object_and_reference_without_holding_state_lock__F0251_F0295(client, monkeypatch):
    import cas
    import database
    from state import state_lock
    rid = install_test_renderer()
    source = _renderer_hash(client, rid)
    code = 'export default () => "ordered publication";'
    digest = hashlib.sha256(code.encode()).hexdigest()
    original = cas.store_bytes
    seen = []

    def observe_store(raw):
        if raw == code.encode():
            assert not state_lock._is_owned(), 'source body IO must run outside the global state lock'
            conn = database.get_global_db_connection()
            assert conn.execute('SELECT 1 FROM renderer_bundles WHERE source_hash=?', (source,)).fetchone() is None
            conn.close()
            seen.append('object')
        return original(raw)

    original_retain = cas.retain_blob
    def observe_retention(value):
        if value == digest:
            assert not state_lock._is_owned(), 'CAS verification must not hold the global state lock'
            assert cas.publication_lock._is_owned(), 'GC cannot detach the referenced object'
            assert Path(cas.blob_path(value)).read_bytes() == code.encode()
            seen.append('reference')
        return original_retain(value)

    original_commit = database.DurableConnection.commit

    def observe_commit(conn):
        database_path = next(row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main')
        if database_path.endswith('/app.db'):
            row = conn.execute('SELECT blob_hash FROM renderer_bundles WHERE source_hash=?', (source,)).fetchone()
            if row is not None and row[0] == digest:
                assert not state_lock._is_owned(), 'global SQL commit must not hold the state lock'
                seen.append('commit')
        return original_commit(conn)

    monkeypatch.setattr(cas, 'store_bytes', observe_store)
    monkeypatch.setattr(cas, 'retain_blob', observe_retention)
    monkeypatch.setattr(database.DurableConnection, 'commit', observe_commit)
    response = client.put(f'/api/renderers/{rid}/bundle', json={'source_hash': source, 'code': code})
    assert response.status_code == 200
    assert seen == ['object', 'reference', 'commit']
