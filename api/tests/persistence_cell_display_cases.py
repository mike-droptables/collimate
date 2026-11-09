"""Actual app display lifecycle acceptance, with fresh browser profiles."""
from __future__ import annotations
import json
import time

from persistence_browser import EMPTY_TAB_OPENER, open_from_files_panel

async def _capture(suite,context,label,channel):
    if context is None:return
    for index,page in enumerate(context.pages):
        try:
            receipt={'body':await page.locator('body').inner_text(),'url':page.url,
                     'display':suite.api('GET',f'/api/channels/{channel}/cell_display')}
            await page.screenshot(path=str(suite.directory/f'{label}-{index}.png'))
            (suite.directory/f'{label}-{index}.json').write_text(json.dumps(receipt,indent=2))
        except Exception as error:
            (suite.directory/f'{label}-{index}-capture-error.txt').write_text(repr(error))

async def _wait(page, predicate, message, seconds=10):
    deadline=time.monotonic()+seconds
    while not predicate():
        assert time.monotonic()<deadline,message
        await page.wait_for_timeout(50)

def _leaves(node):
    return [node] if node['type']=='leaf' else [leaf for child in node['children'] for leaf in _leaves(child)]

async def _split_tabs(suite,page,channel,cell,name,first,second):
    page._persistence_target={'filename':first,'cell':cell}
    await page.locator(f'button[aria-label="{name}"]').last.click()
    host=page.locator(f'[data-cell-id="{cell}"]').last
    opened=host.locator(f'[data-file-path="{first}"]')
    if not(await opened.count() and await opened.is_visible()):
        await host.get_by_role('button',name=EMPTY_TAB_OPENER,exact=True).click()
        await open_from_files_panel(page,first)
    await opened.wait_for(state='visible')
    show=host.get_by_role('button',name='Show files',exact=True)
    if await show.count():await show.click()
    await host.locator(f'[data-filenode-path="{second}"]').dblclick()
    tab=host.locator(f'[role="tab"][title="{second}"]')
    await tab.wait_for(state='visible')
    await tab.click(button='right')
    await page.get_by_role('button',name='Split right',exact=True).click()
    await _wait(page,lambda:len(_leaves(suite.api('GET',f'/api/channels/{channel}/cell_display').get(cell,{'root':{'type':'leaf','tabs':[]}})['root']))==2,'Split display did not reach the server')
    return suite.api('GET',f'/api/channels/{channel}/cell_display')[cell]

async def _assert_restored(page,cell,display):
    host=page.locator(f'[data-cell-id="{cell}"]').last
    deadline=time.monotonic()+10
    expected=[tab for leaf in _leaves(display['root']) for tab in leaf['tabs']]
    while True:
        actual=await host.locator('[data-tab-id]').evaluate_all('elements=>elements.map(element=>element.dataset.tabId)')
        if sorted(actual)==sorted(tab['id'] for tab in expected):break
        assert time.monotonic()<deadline,f'Restored tab IDs differ: {actual!r}'
        await page.wait_for_timeout(50)
    assert await host.get_by_role('tablist').count()==len(_leaves(display['root'])),'Split topology was not restored'
    for leaf in _leaves(display['root']):
        selected=host.locator(f'[data-tab-id="{leaf["activeTabId"]}"] [role="tab"]')
        assert await selected.get_attribute('aria-selected')=='true','Active tab was not restored'
    return {'tabs':len(expected),'leaves':len(_leaves(display['root']))}

async def fresh_profile_restore(suite,browser):
    context,page,channel,cell,errors=await suite.fixture(browser,'first.txt','First body',extra_files=[{'name':'second.txt','content':'Second body'}])
    fresh=None
    try:
        display=await _split_tabs(suite,page,channel,cell,'Persistence probe','first.txt','second.txt')
        # Immediately remove after a display change, before its debounce can
        # commit: the real Remove action must flush the independent owner.
        host=page.locator(f'[data-cell-id="{cell}"]').last
        await host.get_by_role('button',name='Hide files',exact=True).click()
        await host.get_by_role('button',name='Remove cell',exact=True).click()
        await _wait(page,lambda:any(entry['cell_id']==cell for entry in suite.api('GET',f'/api/channels/{channel}/cells/removed')),'Cell did not reach the shelf')
        assert cell not in suite.api('GET',f'/api/channels/{channel}/cell_display'),'Removed live display key was retained'
        await context.close()
        context=None
        fresh=await browser.new_context(viewport={'width':1440,'height':1000})
        assert await fresh.storage_state()=={'cookies':[],'origins':[]},'Fresh profile unexpectedly contains browser state'
        recovered=await fresh.new_page()
        await recovered.goto(suite.base,wait_until='domcontentloaded')
        await recovered.get_by_role('button',name='Zoom out to the overview',exact=True).click()
        await recovered.locator('[data-rm-bar]').wait_for(state='visible')
        await recovered.locator('[data-rm-bar]').click(position={'x':30,'y':15})
        card=recovered.locator(f'[data-rm-card="{cell}"]')
        await card.wait_for(state='visible')
        await card.click()
        await card.get_by_role('button',name='Restore',exact=True).click()
        await _wait(recovered,lambda:cell in suite.api('GET',f'/api/channels/{channel}/cell_display'),'Restored display KV missing')
        restored=suite.api('GET',f'/api/channels/{channel}/cell_display')[cell]
        assert restored['filesPane']['collapsed'] is True,'Remove did not flush the last pane change'
        assert restored['root']==display['root'],'Shelf lifecycle changed tab topology/identities'
        await recovered.get_by_role('button',name='Zoom back in',exact=True).click()
        await recovered.locator('button[aria-label="Persistence probe"]').last.click()
        visible=await _assert_restored(recovered,cell,restored)
        assert await recovered.locator(f'[data-cell-id="{cell}"]').get_by_role('button',name='Show files',exact=True).count()==1
        assert suite.file(channel,cell,'first.txt')=='First body' and suite.file(channel,cell,'second.txt')=='Second body'
        return {'fresh_profile':True,'same_cell_id':cell,'display':visible,'flush_before_remove':True,'current_accepted_files':True,'page_errors':errors}
    finally:
        await _capture(suite,context,'display-restore-original',channel)
        await _capture(suite,fresh,'display-restore-fresh',channel)
        if context:await context.close()
        if fresh:await fresh.close()

async def merge_split_display(suite,browser):
    context,page,channel,first,errors=await suite.fixture(browser,'first.txt','First body',extra_files=[{'name':'second.txt','content':'Second body'}])
    fresh=None
    try:
        first_display=await _split_tabs(suite,page,channel,first,'Persistence probe','first.txt','second.txt')
        made=suite.import_cell(channel,'Other display input',[{'name':'other.txt','content':'Other body'},{'name':'extra.txt','content':'Extra body'}])
        second=made['cell_ids'][0]
        second_display=await _split_tabs(suite,page,channel,second,'Other display input','other.txt','extra.txt')
        state=suite.api('GET','/api/state')
        current=next(c for c in state['channels'] if c['id']==channel)
        first_stack=next(s for s in current['main_stacks'] if any(c['id']==first for c in s['cells']))
        target_index=next(i for i,s in enumerate(current['main_stacks']) if s['id']==first_stack['id'])
        suite.api('POST',f'/api/stacks/{channel}/{made["stack_ids"][0]}/move_cell_to/{target_index}/stack?cell_id={second}&tgt_cell_index=1')
        merged=suite.api('POST',f'/api/channels/{channel}/merge',{'stack_id':first_stack['id']})
        split=suite.api('POST',f'/api/channels/{channel}/split',{'cell_id':merged['new_cell_id']})
        assert set(split['new_cell_ids'])=={first,second},'Unchanged split did not revive original IDs'
        records=suite.api('GET',f'/api/channels/{channel}/cell_display')
        assert records[first]==first_display and records[second]==second_display,'Revival did not preserve the independent displays'
        await context.close()
        context=None
        fresh=await browser.new_context(viewport={'width':1440,'height':1000})
        recovered=await fresh.new_page()
        await recovered.goto(suite.base,wait_until='domcontentloaded')
        outcomes={}
        restored_stack=next(stack for stack in split['channel']['main_stacks'] if stack['id']==split['new_stack_id'])
        for cell,name in [(first,'Persistence probe'),(second,'Other display input')]:
            head=recovered.locator(f'[data-stack-id="{restored_stack["id"]}"] [data-stack-head]')
            await head.click()
            # Split can retain the merged remainder at the front. Navigate the
            # real stack rather than assuming a revived cell is already mounted.
            for _ in restored_stack['cells']:await recovered.keyboard.press('Alt+ArrowUp')
            index=next(i for i,item in enumerate(restored_stack['cells']) if item['id']==cell)
            for _ in range(index):await recovered.keyboard.press('Alt+ArrowDown')
            await recovered.locator(f'button[aria-label="{name}"]').last.click()
            outcomes[cell]=await _assert_restored(recovered,cell,records[cell])
        return {'original_ids_revived':split['new_cell_ids'],'independent_displays':outcomes,'fresh_profile':True,'page_errors':errors}
    finally:
        await _capture(suite,context,'display-merge-original',channel)
        await _capture(suite,fresh,'display-merge-fresh',channel)
        if context:await context.close()
        if fresh:await fresh.close()

async def rename_journal(suite,browser):
    context,page,channel,cell,errors=await suite.fixture(browser)
    try:
        before=suite.api('GET',f'/api/channels/{channel}/cells/{cell}/files')['files']
        suite.api('PATCH',f'/api/channels/{channel}/cells/{cell}/meta',{'name':'New literal name'})
        suite.api('PATCH',f'/api/channels/{channel}/cells/{cell}/meta',{'name':'Current linked name'})
        await page.get_by_role('button',name='Zoom out to the overview',exact=True).click()
        await page.locator('[data-rm-bar]').wait_for(state='visible')
        await page.locator('[data-rm-journal]').click()
        pane=page.get_by_role('complementary',name='Journal')
        await pane.wait_for(state='visible')
        await pane.get_by_text('New literal name',exact=False).first.wait_for(state='visible')
        text=await pane.inner_text()
        assert 'You renamed “Persistence probe” to “New literal name”' in text,'Journal omitted original literal names or user actor'
        assert 'You renamed “New literal name” to “Current linked name”' in text,'Journal resolved historical names through current metadata'
        link=pane.locator(f'button[data-cell-name="{cell}"]').first
        await link.wait_for(state='visible')
        deadline=time.monotonic()+10
        while await link.inner_text()!='Current linked name':
            assert time.monotonic()<deadline,f'Journal current cell reference remained stale: {await link.inner_text()!r}'
            await page.wait_for_timeout(50)
        assert suite.api('GET',f'/api/channels/{channel}/cells/{cell}/files')['files']==before,'Rename changed file revisions'
        return {'literal_historical_names':True,'explicit_user_actor':True,'navigable_current_cell':True,'no_file_revision':True,'page_errors':errors}
    finally:
        await _capture(suite,context,'rename-journal',channel)
        await context.close()

async def run_cell_display_cases(suite,browser):
    await suite.check('display-fresh-profile-shelf-restore',lambda:fresh_profile_restore(suite,browser),gate='P3')
    await suite.check('display-merge-split-revives-ids-and-views',lambda:merge_split_display(suite,browser),gate='P3')
    await suite.check('cell-rename-journal-literal-names-and-actor',lambda:rename_journal(suite,browser),gate='P3')
