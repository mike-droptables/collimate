"""Browser recovery uses channel UUIDs through rename, restart and label reuse."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from urllib.parse import quote, urlparse

from persistence_browser import record_contains
from persistence_safety_cases import _open_other
from persistence_uuid_fixture import inspect as inspect_uuid_fixture


async def _wait(page, predicate, message, seconds=15):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, message
        await page.wait_for_timeout(50)


def _requests(page, rows):
    def capture(request):
        if '/files/mutate' in request.url:
            rows.append({'path': urlparse(request.url).path, 'body': request.post_data})
    page.on('request', capture)


def _restart_manifest(suite, stage):
    manifest = inspect_uuid_fixture(suite.directory)
    connection = sqlite3.connect(f'file:{suite.directory / ".collimate/app.db"}?mode=ro', uri=True)
    try:
        manifest['migration_receipts'] = connection.execute(
            'SELECT * FROM schema_migrations ORDER BY version').fetchall()
    finally:
        connection.close()
    path = suite.directory / f'uuid-{stage}.json'
    path.write_text(json.dumps(manifest, indent=2))
    return manifest, str(path)


async def verify_startup_conversion(suite):
    before, after = suite.uuid_fixture, suite.uuid_after
    for identity, channel in before['semantic']['channels'].items():
        converted = after['semantic']['channels'][identity]
        assert channel == converted, f'Populated channel rows or CAS bytes changed: {identity}'
        assert after['legacy_databases'][identity] == f'{identity}.db'
        old = suite.directory / '.collimate/channels' / before['legacy_databases'][identity]
        assert not old.exists(), 'The runtime retained a legacy database path'
    assert before['semantic']['global'] == after['semantic']['global'], 'Global metadata changed at migration startup'
    connection = sqlite3.connect(f'file:{suite.directory / ".collimate/app.db"}?mode=ro', uri=True)
    try:
        applied = [row[0] for row in connection.execute('SELECT version FROM schema_migrations ORDER BY version')]
    finally:
        connection.close()
    assert applied[-1] == 8
    digest = hashlib.sha256(json.dumps(before['semantic'], sort_keys=True).encode()).hexdigest()
    suite.uuid_conversion = {'global_migration': 8, 'semantic_sha256': digest,
                             'same_semantic_rows_and_bodies': True,
                             'channels': len(before['semantic']['channels']),
                             'before': str(suite.directory / 'uuid-before.json'),
                             'after': str(suite.directory / 'uuid-after.json')}
    return suite.uuid_conversion


async def migrated_rename_restart(suite, browser):
    """The startup fixture was converted before any browser cell was created."""
    prepared = suite.uuid_fixture
    channel, cell = prepared['channel_id'], prepared['cell_id']
    filename, original = prepared['filename'], prepared['body']
    profile = suite.directory / 'uuid-browser-profile'
    context = await browser.browser_type.launch_persistent_context(
        str(profile), viewport={'width': 1440, 'height': 1000})
    page = None
    requests, errors = [], []
    evidence = {'fixture': {key: prepared[key] for key in ('channel_id', 'cell_id', 'filename', 'body')},
                'startup_conversion': suite.uuid_conversion}
    database = suite.directory / '.collimate/channels' / f'{channel}.db'
    inode = database.stat().st_ino
    try:
        context, page, _, _, errors = await suite.fixture(
            browser, filename, original, context=context, existing=(channel, cell))
        _requests(page, requests)
        suite.fault(hold_before=True)
        frame, _, suffix = await suite.type_suffix(page, 'monaco', suffix=' uuid-durable-draft')
        physical = await frame.frame_element()
        await physical.evaluate("element => element.dataset.uuidOwner = 'before-rename'")
        await page.wait_for_timeout(400)
        before = await suite.journal(page, filename)
        evidence['before'] = before
        old_draft = next(row for row in before if record_contains(row, suffix)
                         and row['identity']['channel'] == channel)
        evidence['original_writer'] = old_draft['writerId']
        await _wait(page, lambda: bool(requests), 'No in-flight save was held before rename')
        await _open_other(page, 'sibling.txt')
        await physical.wait_for_element_state('hidden')
        assert await physical.evaluate('element => element.isConnected'), 'Fixture did not retain a parked frame'
        assert not await physical.is_visible(), 'Fixture did not park the dirty renderer'

        renamed = 'UUID renamed ' + uuid.uuid4().hex[:8]
        response = suite.api('PUT', f'/api/channels/{channel}/rename?name={quote(renamed)}')
        assert response['id'] == channel and response['name'] == renamed
        assert database.stat().st_ino == inode, 'A display rename moved or replaced the database'
        await page.get_by_text(renamed, exact=True).first.wait_for(timeout=15000)
        await suite.open_file(page, filename)
        same, _ = await suite.editor(page, 'monaco', filename)
        assert await (await same.frame_element()).get_attribute('data-uuid-owner') == 'before-rename'
        assert suffix in await same.evaluate('window.monaco.editor.getModels()[0].getValue()')

        inventory = suite.api('GET', f'/api/channels/{channel}/cells/{cell}/files')['files']
        current = next(row for row in inventory if row['path'] == filename)
        competing = '# competing accepted body after channel rename\n'
        receipt = suite.api('POST', '/__persistence/host-mutate', {
            'channel': channel, 'cell_id': cell,
            'operation': {'operation_id': str(uuid.uuid4()), 'mutations': [{
                'path': filename, 'writer_id': 'uuid-browser:competing', 'edit_seq': 1,
                'base_revision': current['file_revision'], 'kind': 'put',
                'content_text': competing, 'language': 'python'}], 'resolves': []}})
        assert receipt['outcome'] == 'accepted'
        suite.fault()
        conflicts_url = f'/api/channels/{channel}/cells/{cell}/files/conflicts'

        def candidates():
            return [row for row in suite.api('GET', conflicts_url)['conflicts']
                    if row['path'] == filename]

        await _wait(page, lambda: bool(candidates()), 'The held save did not preserve a conflict candidate')
        retained = candidates()
        evidence['conflicts_before_restart'] = retained
        unrelated = [row for row in suite.api('GET', conflicts_url)['conflicts']
                     if row['path'] == 'conflict.txt']
        unrelated_body = suite.file(channel, cell, 'conflict.txt')
        evidence['unrelated_conflicts'] = unrelated
        assert unrelated, 'The prepared fixture must retain its unrelated conflict'
        before_restart = await suite.journal(page, filename)
        evidence['before_restart'] = before_restart
        assert any(record_contains(row, suffix) and row['identity']['channel'] == channel
                   for row in before_restart)
        previous_pid = suite.process.pid
        await context.close()
        context = None
        await suite.stop()
        frozen, frozen_path = _restart_manifest(suite, 'after-rename-before-restart')
        evidence['after_rename_manifest'] = frozen_path
        suite.fault(refuse=True)
        await suite.start()
        assert suite.process.pid != previous_pid
        assert database.stat().st_ino == inode
        restarted, restarted_path = _restart_manifest(suite, 'first-restart')
        evidence['first_restart_manifest'] = restarted_path
        assert restarted == frozen, 'Restart changed populated semantic rows, CAS or migration receipts'
        restarted_pid = suite.process.pid
        await suite.stop()
        await suite.start()
        assert suite.process.pid not in (previous_pid, restarted_pid)
        assert database.stat().st_ino == inode
        repeated, repeated_path = _restart_manifest(suite, 'second-restart')
        evidence['second_restart_manifest'] = repeated_path
        assert repeated == frozen, 'Repeated startup changed the converted store or migration receipts'
        assert suite.file(channel, cell, filename) == competing
        after_candidates = candidates()
        evidence['conflicts_after_restart'] = after_candidates
        assert after_candidates == retained, 'Restart changed the preserved conflict candidates'
        context = await browser.browser_type.launch_persistent_context(
            str(profile), viewport={'width': 1440, 'height': 1000})
        page = await context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        _requests(page, requests)
        page._persistence_target = {'filename': filename, 'cell': cell}
        await page.goto(suite.base, wait_until='domcontentloaded')
        await suite.open_file(page, filename)
        recovered, _ = await suite.editor(page, 'monaco', filename)
        after = await suite.journal(page, filename)
        evidence['after_restart_before_recovery'] = after
        assert any(record_contains(row, suffix) and row['identity'] == old_draft['identity']
                   and row['writerId'] == old_draft['writerId'] for row in after), \
            'Browser process restart lost the original UUID/writer draft'
        new_writer = await page.evaluate("sessionStorage.getItem('collimate.documentWriter')")
        evidence['recovery_window_namespace'] = new_writer
        assert not old_draft['writerId'].startswith(f'{new_writer}:')
        assert await recovered.evaluate('window.monaco.editor.getModels()[0].getValue()') == competing
        editor = page.locator(f'[data-file-path="{filename}"]:visible').first
        # A new browser process has a new session writer. Explicit adoption
        # preserves the old writer's journal and never silently steals its text.
        await editor.get_by_role('button', name='Recover its text', exact=True).click()
        await recovered.wait_for_function(
            'suffix => window.monaco?.editor.getModels()[0]?.getValue().includes(suffix)', arg=suffix)
        deadline = time.monotonic() + 10
        while True:
            after = await suite.journal(page, filename)
            adopted = [row for row in after if row['writerId'].startswith(f'{new_writer}:')
                       and record_contains(row, suffix)]
            if adopted:
                break
            assert time.monotonic() < deadline, 'Explicit recovery did not journal its new document owner'
            await page.wait_for_timeout(50)
        assert len(adopted) == 1
        evidence['recovery_writer'] = adopted[0]['writerId']
        assert evidence['recovery_writer'] != old_draft['writerId']
        evidence['after'] = after
        assert any(record_contains(row, suffix) and row['identity']['channel'] == channel
                   for row in after), 'Browser process restart lost the UUID draft'
        await editor.get_by_role('button', name='Review versions', exact=True).wait_for(timeout=15000)
        await editor.get_by_role('button', name='Review versions', exact=True).click()
        choices = editor.get_by_role('combobox', name='Left version')
        await choices.wait_for(state='visible')
        labels = await choices.locator('option').all_text_contents()
        assert all(any(candidate['writer_id'] in label and f"edit {candidate['edit_seq']}" in label
                       and f"revision {candidate['candidate_revision']}" in label for label in labels)
                   for candidate in retained), 'Review omitted the preserved writer and edit sequence'
        assert all('undefined' not in label for label in labels)
        assert suite.file(channel, cell, filename) == competing
        suite.fault()
        await editor.get_by_role('button', name='Keep my edits', exact=True).click()
        await _wait(page, lambda: suite.file(channel, cell, filename) == original + suffix,
                    'Explicit resolution did not save the recovered draft')
        assert not candidates(), 'Explicit resolution left the reviewed conflict active'
        assert [row for row in suite.api('GET', conflicts_url)['conflicts']
                if row['path'] == 'conflict.txt'] == unrelated
        assert suite.file(channel, cell, 'conflict.txt') == unrelated_body
        resolution = [json.loads(row['body']) for row in requests
                      if json.loads(row['body']).get('resolves')]
        assert len(resolution) == 1
        assert set(resolution[0]['resolves']) == {row['conflict_id'] for row in retained}
        assert all(row['path'].startswith(f'/api/channels/{channel}/') for row in requests)
        evidence.update({'same_uuid': channel, 'database_inode_preserved': True,
                         'parked_renderer_retained': True, 'pending_save_captured': bool(requests),
                         'server_process_restarted': True, 'browser_process_restarted': True,
                         'explicit_conflict_resolution': True, 'before': before, 'after': after,
                         'requests': requests, 'page_errors': errors, 'renderer': 'monaco'})
        return evidence
    finally:
        evidence.update({'requests': requests, 'page_errors': errors})
        if page and not page.is_closed():
            evidence['body'] = await page.locator('body').inner_text()
        elif context:
            evidence['pages'] = [{'url': item.url, 'body': await item.locator('body').inner_text()}
                                 for item in context.pages if not item.is_closed()]
        (suite.directory / 'uuid-rename-recovery.json').write_text(json.dumps(evidence, indent=2))
        suite.fault()
        if context:
            await context.close()


async def deleted_label_reuse(suite, browser):
    context, page, old, cell, errors = await suite.fixture(browser)
    requests = []
    evidence = {'old_uuid': old}
    _requests(page, requests)
    try:
        label = next(row['name'] for row in suite.api('GET', '/api/state')['channels'] if row['id'] == old)
        suite.fault(hold_before=True)
        _, _, suffix = await suite.type_suffix(page, suffix=' deleted-channel-only')
        await page.wait_for_timeout(400)
        assert any(record_contains(row, suffix) and row['identity']['channel'] == old
                   for row in await suite.journal(page, 'notes.txt'))
        await _wait(page, lambda: bool(requests), 'No pending old-channel request was observed')
        suite.api('DELETE', f'/api/channels/{old}')
        created = suite.create_seeded_channel(label)
        new = created['id']
        evidence.update({'new_uuid': new, 'same_display_label': label})
        assert new != old and created['name'] == label
        made = suite.import_cell(new, 'Persistence probe', [{'name': 'notes.txt', 'content': 'new channel bytes'}])
        suite.fault()
        await page.reload(wait_until='domcontentloaded')
        page._persistence_target = {'filename': 'notes.txt', 'cell': made['cell_ids'][0]}
        await suite.open_file(page, 'notes.txt')
        _, editor = await suite.editor(page)
        assert await editor.input_value() == 'new channel bytes'
        records = await suite.journal(page, 'notes.txt')
        assert all(row['identity']['channel'] == old for row in records if record_contains(row, suffix))
        assert not any(row['path'].startswith(f'/api/channels/{new}/') and suffix in row['body']
                       for row in requests), 'An old operation was retargeted to the reused label'
        _, _, new_suffix = await suite.type_suffix(page, suffix=' new-owner')
        evidence['new_owner_editor_after_typing'] = await editor.input_value()
        evidence['new_owner_journal_after_typing'] = await suite.journal(page, 'notes.txt')
        await _wait(page, lambda: suite.file(new, made['cell_ids'][0], 'notes.txt') == 'new channel bytes' + new_suffix,
                    'The new UUID document could not save independently')
        return {'old_uuid': old, 'new_uuid': new, 'same_display_label': label,
                'pending_requests_keep_old_identity': True, 'independent_recovery': True,
                'requests': requests, 'page_errors': errors, 'renderer': 'text'}
    finally:
        evidence.update({'requests': requests, 'page_errors': errors,
                         'body': await page.locator('body').inner_text(),
                         'journal': await suite.journal(page, 'notes.txt')})
        (suite.directory / 'uuid-delete-label-reuse.json').write_text(json.dumps(evidence, indent=2))
        suite.fault()
        await context.close()


async def run_uuid_cases(suite, browser):
    await suite.check('uuid-populated-startup-conversion',
                      lambda: verify_startup_conversion(suite), gate='P4')
    await suite.check('uuid-migrated-rename-restart-recovery',
                      lambda: migrated_rename_restart(suite, browser), gate='P4')
    await suite.check('uuid-delete-recreate-label-isolation',
                      lambda: deleted_label_reuse(suite, browser), gate='P4')
