"""Ordering and navigation faults against real file owners and the built app."""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid


async def late_ack(suite, browser):
    context, page, channel, cell, errors = await suite.fixture(browser)
    try:
        suite.fault(delay_after=2)
        _, editor, first = await suite.type_suffix(page, suffix=' older-flight')
        deadline = time.monotonic() + 10
        while first not in suite.file(channel, cell, 'notes.txt'):
            assert time.monotonic() < deadline, 'First write did not reach SQL before its held response'
            await page.wait_for_timeout(50)
        accepted_before = suite.file(channel, cell, 'notes.txt')
        suite.fault(refuse=True)
        await editor.press_sequentially(' newer-keys', delay=30)
        expected = await editor.input_value()
        await page.wait_for_timeout(2300)
        assert await editor.input_value() == expected, 'An older acknowledgement replaced current typing'
        records = await suite.journal(page, 'notes.txt')
        current = next(record for record in records if record['body'] == expected)
        assert current['editSeq'] > current['savedSeq'], 'Old acknowledgement marked newer keys saved'
        assert suite.file(channel, cell, 'notes.txt') == accepted_before
        await page.get_by_text('Server save failed', exact=True).wait_for(timeout=10000)
        suite.fault()
        await page.get_by_role('button', name='Retry save', exact=True).click()
        await page.wait_for_timeout(600)
        assert suite.file(channel, cell, 'notes.txt') == expected
        return {'renderer': 'text', 'acknowledgement_delayed_seconds': 2,
                'late_ack_keeps_newer_edit_dirty': True, 'page_errors': errors}
    finally:
        await context.close()


async def during_typing(suite, browser, kind='text'):
    from persistence_browser import record_contains
    filename = 'notes.md' if kind == 'milkdown' else 'notes.txt'
    context, page, channel, cell, errors = await suite.fixture(browser, filename)
    typing = None
    stop = asyncio.Event()
    try:
        suite.fault(refuse=True)
        frame, editor = await suite.editor(page, kind)
        ready_deadline=time.monotonic()+10
        while True:
            initial=await editor.inner_text() if kind=='milkdown' else await editor.input_value()
            if initial.strip()=='original':break
            assert time.monotonic()<ready_deadline,'Initial owner body was not ready before active input'
            await page.wait_for_timeout(50)
        await editor.focus()
        await editor.press('Control+End')
        async def stream_keys():
            await editor.press_sequentially(' during-typing-',delay=60)
            deadline=time.monotonic()+45
            index=0
            while not stop.is_set() and time.monotonic()<deadline:
                await editor.press_sequentially(str(index%10),delay=60)
                index+=1
        typing=asyncio.create_task(stream_keys())
        # A receipt, not a sleep after the final keystroke, is the guarantee.
        durable = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            records = await suite.journal(page, filename)
            durable = next((record for record in records if record_contains(record, 'during-typing-')), None)
            if durable:
                break
            if typing.done():break
            await page.wait_for_timeout(20)
        visible_before=await editor.inner_text() if kind=='milkdown' else await editor.input_value()
        task_error=repr(typing.exception()) if typing.done() and not typing.cancelled() else None
        evidence={'renderer':kind,'visible_before_reload':visible_before,'records':records,
                  'typing_done':typing.done(),'typing_error':task_error,'page_errors':errors,
                  'metrics':await frame.evaluate('window.__persistenceMetrics'),
                  'host_metrics':await page.evaluate('window.__persistenceMetrics')}
        (suite.directory/f'during-typing-boundary-{kind}.json').write_text(json.dumps(evidence,indent=2))
        assert durable and not typing.done(), 'Fixture did not interrupt an active typing stream after local commit'
        await page.reload(wait_until='domcontentloaded')
        stop.set()
        with contextlib.suppress(Exception):
            await typing
        await suite.open_file(page, filename)
        deadline=time.monotonic()+5
        while True:
            _, recovered = await suite.editor(page, kind)
            visible = await recovered.inner_text() if kind == 'milkdown' else await recovered.input_value()
            if 'during-typing-' in visible or time.monotonic()>=deadline:break
            await page.wait_for_timeout(50)
        assert 'during-typing-' in visible, 'Navigation during typing lost an acknowledged local prefix'
        records_after = await suite.journal(page, filename)
        assert any(record_contains(record, 'during-typing-') and record['editSeq'] >= durable['editSeq']
                   for record in records_after)
        assert 'during-typing-' not in suite.file(channel, cell, filename)
        return {'renderer': kind, 'reload_during_active_keys': True,
                'last_observed_local_receipt_sequence': durable['editSeq'],
                'guarantee': 'All keys in the observed local transaction survive; later unacknowledged keys are not assumed durable.',
                'page_errors': errors}
    finally:
        stop.set()
        if typing and not typing.done():
            typing.cancel()
        await context.close()


async def older_delivery(suite, browser):
    context, page, channel, cell, errors = await suite.fixture(browser)
    held = asyncio.Event()
    release = asyncio.Event()
    captured = []
    receipts = []

    async def hold_first(route):
        payload = route.request.post_data_json
        if not captured:
            captured.append(payload)
            held.set()
            await release.wait()
        response = await route.fetch()
        receipts.append(await response.json())
        await route.fulfill(response=response)

    try:
        await page.route('**/files/mutate', hold_first)
        _, editor, suffix = await suite.type_suffix(page, suffix=' held-in-transit')
        await asyncio.wait_for(held.wait(), 10)
        request = captured[0]
        first = request['mutations'][0]
        # Deliver a later operation from the same writer before the held one.
        # This uses the real service; it does not invoke the client owner.
        newer = {**first, 'edit_seq': first['edit_seq'] + 1, 'content_text': 'newer network delivery'}
        result = suite.api('POST', f'/api/channels/{channel}/cells/{cell}/files/mutate', {
            'operation_id': uuid.uuid4().hex, 'mutations': [newer], 'resolves': []})
        assert result['outcome'] == 'accepted'
        release.set()
        await page.get_by_role('button', name='Review versions', exact=True).first.wait_for(timeout=10000)
        await page.wait_for_timeout(300)
        assert receipts and receipts[0]['outcome'] == 'superseded', 'The older request was not delivered last'
        assert suffix in await editor.input_value()
        assert suite.file(channel, cell, 'notes.txt') == 'newer network delivery'
        assert any(suffix in record['body'] for record in await suite.journal(page, 'notes.txt'))
        return {'renderer': 'text', 'older_request_outcome': receipts[0]['outcome'],
                'held_request_id': request['operation_id'], 'newer_revision': result['files'][0]['file_revision'],
                'local_buffer_and_server_version_preserved': True, 'page_errors': errors}
    finally:
        release.set()
        await context.close()


async def deletion(suite, browser):
    context, page, channel, cell, errors = await suite.fixture(browser)
    try:
        suite.fault(refuse=True)
        _, editor, suffix = await suite.type_suffix(page, suffix=' survives-delete')
        await page.wait_for_timeout(400)
        inventory = suite.api('GET', f'/api/channels/{channel}/cells/{cell}/files')['files']
        current = next(file for file in inventory if file['path'] == 'notes.txt')
        suite.fault()
        deleted = suite.api('POST', f'/api/channels/{channel}/cells/{cell}/files/mutate', {
            'operation_id': uuid.uuid4().hex, 'mutations': [{'path': 'notes.txt',
                'writer_id': uuid.uuid4().hex, 'edit_seq': 1, 'base_revision': current['file_revision'],
                'kind': 'delete'}], 'resolves': []})
        tombstone = deleted['files'][0]['file_revision']
        await page.get_by_role('button', name='Review versions', exact=True).first.wait_for(timeout=10000)
        await page.wait_for_timeout(600)
        assert suffix in await editor.input_value()
        assert next(file for file in suite.api('GET', f'/api/channels/{channel}/cells/{cell}/files')['files']
                    if file['path'] == 'notes.txt')['deleted'], 'A delayed save silently recreated the deleted path'
        await page.get_by_role('button', name='Review versions', exact=True).first.click()
        await page.get_by_role('button', name='Keep my edits', exact=True).click()
        await page.wait_for_timeout(600)
        recreated = next(file for file in suite.api('GET', f'/api/channels/{channel}/cells/{cell}/files')['files']
                         if file['path'] == 'notes.txt')
        assert not recreated['deleted'] and recreated['file_revision'] > tombstone
        assert suffix in suite.file(channel, cell, 'notes.txt')
        return {'renderer': 'text', 'tombstone_revision': tombstone,
                'explicit_recreation_revision': recreated['file_revision'], 'page_errors': errors}
    finally:
        await context.close()


async def uncommitted_reload(suite, browser):
    context, page, channel, cell, errors = await suite.fixture(browser)
    try:
        suite.fault(refuse=True)
        await page.evaluate('''() => {
            const put = IDBObjectStore.prototype.put;
            IDBObjectStore.prototype.put = function(...args) {
                const request = put.apply(this, args);
                if (this.name === 'documents') this.transaction.abort();
                return request;
            };
        }''')
        _, editor, suffix = await suite.type_suffix(page, suffix=' uncommitted-key')
        await page.get_by_text('Local save failed', exact=True).wait_for(timeout=10000)
        assert suffix in await editor.input_value()
        assert not await suite.journal(page, 'notes.txt')
        assert suffix not in suite.file(channel, cell, 'notes.txt')
        await page.reload(wait_until='domcontentloaded')
        await suite.open_file(page, 'notes.txt')
        _, recovered = await suite.editor(page)
        assert await recovered.input_value() == 'original '
        return {'renderer': 'text', 'local_transaction': 'aborted before completion',
                'failure_visible_before_reload': True,
                'observed_loss': 'Uncommitted keys are lost on reload; no local or server save was acknowledged.',
                'page_errors': errors}
    finally:
        await context.close()


async def conflict_rename(suite, browser):
    context, page, channel, cell, errors = await suite.fixture(browser)
    try:
        await page.get_by_role('button', name='Show files', exact=True).last.click()
        suite.fault(refuse=True)
        _, editor, suffix = await suite.type_suffix(page, suffix=' conflict-before-rename')
        await page.wait_for_timeout(400)
        suite.fault()
        suite.mutate(channel, cell, 'notes.txt', 'competing writer before rename')
        await page.get_by_role('button', name='Review versions', exact=True).first.wait_for(timeout=10000)
        row = page.locator('[data-filenode-path="notes.txt"]')
        await row.hover()
        await row.get_by_title('Rename', exact=True).click()
        field = row.locator('input')
        await field.fill('renamed.txt')
        await field.press('Enter')
        await page.get_by_text('Review versions before renaming this file.', exact=True).wait_for(timeout=10000)
        assert suffix in await editor.input_value()
        assert not any(file['path'] == 'renamed.txt' for file in
                       suite.api('GET', f'/api/channels/{channel}/cells/{cell}/files')['files'])
        assert suite.file(channel, cell, 'notes.txt') == 'competing writer before rename'
        return {'renderer': 'text', 'rename_refuses_unresolved_conflict': True,
                'both_versions_preserved': True, 'page_errors': errors}
    finally:
        await context.close()


async def middle_caret(suite, browser, kind):
    filename = {'text':'notes.txt', 'milkdown':'notes.md', 'monaco':'notes.py'}[kind]
    context, page, channel, cell, errors = await suite.fixture(browser, filename,
        'alpha beta gamma\n\ndelta epsilon', extra_files=[{'name':'other.txt','content':'sibling'}])
    try:
        frame, editor = await suite.editor(page, kind)
        await editor.focus()
        await editor.press('Control+Home')
        for _ in range(5):
            await editor.press('ArrowRight')
        handle = await editor.element_handle()
        await editor.press_sequentially('X')
        deadline = time.monotonic() + 10
        while 'alphaX beta' not in suite.file(channel, cell, filename):
            assert time.monotonic() < deadline, 'Initial middle edit did not reach its accepted revision'
            await page.wait_for_timeout(50)
        suite.api('POST', f'/api/channels/{channel}/cells/{cell}/checkpoint')
        suite.mutate(channel, cell, 'other.txt', 'unrelated accepted change')
        await page.wait_for_timeout(600)
        assert await handle.evaluate('node=>node.isConnected'), 'Save/checkpoint replaced the editor node'
        # Subsequent real typing proves the logical caret survived; textarea
        # offsets alone would not cover ProseMirror or Monaco selections.
        await editor.press_sequentially('Y')
        deadline = time.monotonic() + 10
        while 'alphaXY beta' not in suite.file(channel, cell, filename):
            assert time.monotonic() < deadline, 'Save/checkpoint moved the caret away from its middle position'
            await page.wait_for_timeout(50)
        return {'renderer':kind, 'middle_caret_after_ack_checkpoint_and_sibling_write':True,
                'same_editor_node':True, 'page_errors':errors}
    finally:
        await context.close()


async def run_ordering_cases(suite, browser):
    await suite.check('late-ack-keeps-newer-typing-dirty', lambda: late_ack(suite, browser))
    await suite.check('older-request-delivered-last', lambda: older_delivery(suite, browser))
    await suite.check('delete-dirty-document-explicit-recreate', lambda: deletion(suite, browser))
    await suite.check('reload-before-local-transaction-receipt', lambda: uncommitted_reload(suite, browser))
    await suite.check('conflict-must-resolve-before-rename', lambda: conflict_rename(suite, browser))
    for kind in ['text', 'milkdown']:
        await suite.check(f'reload-during-typing-{kind}', lambda kind=kind: during_typing(suite, browser, kind))
    for kind in ['text', 'milkdown', 'monaco']:
        await suite.check(f'middle-caret-survives-ack-checkpoint-{kind}', lambda kind=kind: middle_caret(suite, browser, kind))
    from persistence_view_identity import rename_and_recreate_source
    await suite.check('rename-recreate-source-has-new-view-owner',
                      lambda: rename_and_recreate_source(suite, browser))
