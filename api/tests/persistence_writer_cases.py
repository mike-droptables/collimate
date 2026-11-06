"""Actual-browser regressions for document writer sequence ownership."""
from __future__ import annotations

import json
import time
import uuid


def descriptor(suite, channel, cell, path):
    return next(row for row in suite.api(
        'GET', f'/api/channels/{channel}/cells/{cell}/files')['files'] if row['path'] == path)


async def saved(suite, page, channel, cell, path, expected):
    deadline = time.monotonic() + 12
    while True:
        if suite.file(channel, cell, path) == expected and not await suite.journal(page, path):
            return descriptor(suite, channel, cell, path)
        assert time.monotonic() < deadline, 'Latest edit did not receive acceptance and clear its draft'
        await page.wait_for_timeout(50)


async def clean_reload(suite, browser):
    path, original = 'notes.txt', 'original '
    context, page, channel, cell, errors = await suite.fixture(browser, path, original)
    evidence = {'page_errors': errors}
    try:
        _, _, suffix = await suite.type_suffix(page, suffix='first-session-has-many-edits')
        prior = await saved(suite, page, channel, cell, path, original + suffix)
        window_before = await page.evaluate("sessionStorage.getItem('collimate.documentWriter')")
        evidence['before'] = prior
        await page.reload(wait_until='domcontentloaded')
        await suite.open_file(page, path)
        _, editor = await suite.editor(page)
        assert await editor.input_value() == original + suffix
        assert await page.evaluate("sessionStorage.getItem('collimate.documentWriter')") == window_before
        await editor.focus()
        await editor.press('Control+End')
        await editor.press_sequentially('z')
        current = await saved(suite, page, channel, cell, path, original + suffix + 'z')
        evidence['after'] = current
        assert current['writer_id'] != prior['writer_id'], 'Clean reload reused the completed sequence domain'
        assert current['edit_seq'] < prior['edit_seq'], 'Probe did not exercise a lower new-session sequence'
        assert not errors
        return {'renderer': 'text', 'same_window_namespace': True,
                'clean_draft_before_reload': True, 'fresh_writer_after_reload': True,
                'single_new_edit_accepted': True, **evidence}
    finally:
        evidence['journal'] = await suite.journal(page, path)
        (suite.directory / 'writer-clean-reload.json').write_text(json.dumps(evidence, indent=2))
        await context.close()


async def rename_to_used_target(suite, browser):
    target, source = 'notes.txt', 'source.txt'
    context, page, channel, cell, errors = await suite.fixture(
        browser, target, 'target ', extra_files=[{'name': source, 'content': 'source '}])
    evidence = {'page_errors': errors}
    try:
        _, _, suffix = await suite.type_suffix(page, suffix='prior-target-has-many-edits')
        prior = await saved(suite, page, channel, cell, target, 'target ' + suffix)
        evidence['prior_target'] = prior
        await suite.open_file(page, source)
        await suite.type_suffix(page, suffix='x')
        source_owner = await saved(suite, page, channel, cell, source, 'source x')
        evidence['source_owner'] = source_owner
        deleted = suite.api('POST', f'/api/channels/{channel}/cells/{cell}/files/mutate', {
            'operation_id': str(uuid.uuid4()), 'mutations': [{
                'path': target, 'base_revision': prior['file_revision'],
                'writer_id': 'torture:delete-used-target', 'edit_seq': 1, 'kind': 'delete'}]})
        assert deleted['outcome'] == 'accepted'
        await page.get_by_role('button', name='Show files', exact=True).last.click()
        await page.locator(f'[data-filenode-path="{target}"]').wait_for(state='detached')
        row = page.locator(f'[data-filenode-path="{source}"]')
        await row.hover()
        await row.get_by_title('Rename', exact=True).click()
        field = row.locator('input')
        await field.fill(target)
        await field.press('Enter')
        deadline = time.monotonic() + 12
        while not descriptor(suite, channel, cell, source)['deleted']:
            assert time.monotonic() < deadline, 'Rename to the deleted target did not complete'
            await page.wait_for_timeout(50)
        await suite.open_file(page, target)
        _, editor = await suite.editor(page)
        assert await editor.input_value() == 'source x'
        await editor.focus()
        await editor.press('Control+End')
        await editor.press_sequentially('z')
        current = await saved(suite, page, channel, cell, target, 'source xz')
        evidence['after'] = current
        assert current['writer_id'] == source_owner['writer_id'], 'Rename replaced its document writer'
        assert current['writer_id'] != prior['writer_id']
        assert current['edit_seq'] < prior['edit_seq'], 'Probe did not exercise the old target highwater'
        assert not errors
        return {'renderer': 'text', 'used_target_tombstoned': True,
                'rename_preserved_writer': True, 'single_new_edit_accepted': True, **evidence}
    finally:
        evidence['target_journal'] = await suite.journal(page, target)
        evidence['source_journal'] = await suite.journal(page, source)
        (suite.directory / 'writer-rename-used-target.json').write_text(json.dumps(evidence, indent=2))
        await context.close()


async def run_writer_cases(suite, browser):
    await suite.check('writer-clean-accepted-reload-new-edit', lambda: clean_reload(suite, browser), gate='P4')
    await suite.check('writer-rename-previously-edited-target', lambda: rename_to_used_target(suite, browser), gate='P4')
