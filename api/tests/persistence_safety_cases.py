"""Browser safety gates over real shipped hosts, documents and HTTP services.

The declared-control fixture registers a synthetic surface and session socket,
without a generator manager. It proves dispatch and reports the real 409;
successful generator consumption belongs to the separate run integration gate.
"""
from __future__ import annotations

import time
import uuid

from persistence_browser import EMPTY_TAB_OPENER, open_from_files_panel, record_contains
from persistence_renderer_cases import _find, _close


async def _choose_renderer(page, filename, renderer_name):
    tab=page.locator(f'button[role="tab"][title*="{filename}"]').last
    await tab.click(button='right')
    await page.get_by_role('button',name='Open with',exact=True).last.hover()
    await page.get_by_role('menuitem',name=renderer_name,exact=False).click()


def _host_files(suite,channel,cell,files):
    operation={'operation_id':str(uuid.uuid4()),'mutations':[{'path':file['name'],'writer_id':'host:safety-fixture','edit_seq':index+1,'base_revision':0,'kind':'put','content_text':file['content'],'language':'plaintext'} for index,file in enumerate(files)],'resolves':[]}
    result=suite.api('POST','/__persistence/host-mutate',{'channel':channel,'cell_id':cell,'operation':operation})
    assert result['outcome']=='accepted',result


async def _open_other(page,path):
    await page.get_by_role('button',name='New tab',exact=True).last.click()
    await page.get_by_role('button',name=EMPTY_TAB_OPENER,exact=True).last.click()
    await open_from_files_panel(page,path)
    focus=page.get_by_title('Click to focus',exact=True)
    if await focus.count() and await focus.last.is_visible():await focus.last.click()


async def _run_fixture(suite,browser,path,body,context=None,extra_files=None):
    context,page,channel,cell,errors=await suite.fixture(browser,'safety-seed.txt','Fixture seed',context=context)
    _host_files(suite,channel,cell,[{'name':path,'content':body},*(extra_files or [])])
    await page.wait_for_timeout(300)
    await _open_other(page,path)
    page._persistence_target={'filename':path,'cell':cell}
    return context,page,channel,cell,errors


async def renderer_switch(suite,browser):
    filename='switch.md'
    context,page,channel,cell,errors=await suite.fixture(browser,filename,'original text ')
    try:
        suite.fault(refuse=True)
        frame,editor,suffix=await suite.type_suffix(page,'milkdown',suffix=' switch-'+uuid.uuid4().hex[:8])
        await page.wait_for_timeout(400)
        before=await suite.journal(page,filename)
        assert any(record_contains(record,suffix) for record in before),'Switch has no durable raw snapshot'
        # A deliberate renderer switch mounts a different editor. Selection
        # portability across different editor implementations is not promised.
        await _choose_renderer(page,filename,'Monaco Editor')
        monaco_frame,monaco=await suite.editor(page,'monaco')
        assert suffix.strip() in await monaco.evaluate("el=>el.closest('.monaco-editor').textContent"),'Switch to Monaco lost dirty Markdown'
        await monaco.press('Control+End')
        token=' monaco-'+uuid.uuid4().hex[:8]
        await monaco.press_sequentially(token,delay=50)
        await page.wait_for_timeout(400)
        after_monaco=await suite.journal(page,filename)
        assert any(record_contains(record,token) for record in after_monaco),'Monaco did not hand its latest text to the durable owner'
        await _choose_renderer(page,filename,'Milkdown Editor')
        # The previous pooled frame can remain visible during the asynchronous
        # handoff. Wait for the new lease's body rather than inspecting it in
        # the menu click's first turn.
        deadline=time.monotonic()+45
        while True:
            _,recovered=await suite.editor(page,'milkdown')
            text=await recovered.inner_text()
            if suffix.strip() in text and token.strip() in text:break
            assert time.monotonic()<deadline,f'Switch back lost latest owner: {text!r}; journal={await suite.journal(page,filename)!r}'
            await page.wait_for_timeout(100)
        await recovered.press('Control+End')
        await recovered.press_sequentially(' caret-tail',delay=35)
        await page.wait_for_timeout(400)
        assert (await recovered.inner_text()).endswith('caret-tail'),'Restored editor cannot place subsequent typing at its chosen caret'
        assert any(record_contains(record,'caret-tail') for record in await suite.journal(page,filename))
        assert suffix not in suite.file(channel,cell,filename),'Refused switch unexpectedly saved remotely'
        return {'renderer_sequence':['milkdown','monaco','milkdown'],'dirty_buffer_preserved':True,
                'caret_scope':'Explicit renderer switches reset implementation-specific selection; subsequent end-caret typing is checked.',
                'raw_snapshot_before_switch':before,'page_errors':errors}
    finally:await _close(suite,context,page,'safety-renderer-switch')


async def run_primary_readonly(suite,browser,role):
    native=role=='native'
    filename='_runs/safety/source.txt' if native else '_runs/safety/source.md'
    original='Run-owned source remains unchanged.'
    context,page,channel,cell,errors=await _run_fixture(suite,browser,filename,original)
    try:
        if role=='standalone':
            # The fixture opens a newly seeded run path. Wait for its inline
            # lease before asking that renderer to hand its document off.
            # Clicking during the loading placeholder cannot exercise a
            # standalone renderer's read-only contract.
            await _find(page,'.ProseMirror')
            await page.get_by_role('button',name='Expand the cell view to the full window',exact=True).last.click()
            iframe=page.locator('[data-standalone-renderer] iframe').last
            await iframe.wait_for(state='visible')
            node=await iframe.element_handle()
            frame=await node.content_frame()
            control=frame.locator('.ProseMirror').first
            await control.wait_for(state='visible')
        else:frame,control=await _find(page,'textarea' if native else '.ProseMirror')
        if native:assert await control.is_disabled() or await control.get_attribute('readonly') is not None,'Native run primary exposes an editable textarea'
        else:
            assert await control.get_attribute('contenteditable')=='false','Iframe run primary exposes an editable PM document'
            frame_node=await frame.frame_element()
            standalone=await frame_node.evaluate("element=>!!element.closest('[data-standalone-renderer]')")
            assert standalone==(role=='standalone'),'Wrong physical iframe host exercised'
        before=await control.input_value() if native else await control.inner_text()
        await control.click(force=True)
        await page.keyboard.press('Control+End')
        await page.keyboard.type(' forbidden-primary-edit',delay=30)
        await page.wait_for_timeout(500)
        after=await control.input_value() if native else await control.inner_text()
        assert after==before,'Run primary UI accepted typing'
        assert suite.file(channel,cell,filename)==original,'Run primary typing altered accepted server bytes'
        assert not any(record['editSeq']>record['savedSeq'] for record in await suite.journal(page,filename)),'Read-only typing created a dirty primary owner'
        return {'host_role':role,'renderer':'text' if native else 'milkdown','run_phase':'inactive stored run-owned path',
                'primary_ui_readonly':True,'keyboard_edit_refused':True,'page_errors':errors}
    finally:await _close(suite,context,page,'safety-run-primary-'+role)


async def sustained_with_run_writes(suite,browser):
    document='_runs/safety/status.html'
    context,page,channel,cell,errors=await suite.fixture(browser)
    _host_files(suite,channel,cell,[{'name':document,'content':'<h1 id="goal">Initial run document</h1>'}])
    try:
        frame,editor=await suite.editor(page)
        deadline=time.monotonic()+10
        while await editor.input_value()!='original ':
            assert time.monotonic()<deadline,'Initial native body did not load before the sustained input case'
            await page.wait_for_timeout(100)
        await editor.focus()
        await editor.press('Control+End')
        await suite.record_frames(editor)
        prefix=' concurrent-'+uuid.uuid4().hex[:8]+'-'
        await editor.press_sequentially(prefix,delay=20)
        started=time.monotonic()
        checkpoints=[]
        run_receipts=[]
        for index in range(110):
            await editor.press_sequentially(str(index%10),delay=100)
            if index in (15,45,75):
                inventory=suite.api('GET',f'/api/channels/{channel}/cells/{cell}/files')['files']
                head=next(file for file in inventory if file['path']==document)
                operation={'operation_id':str(uuid.uuid4()),'mutations':[{'path':document,'writer_id':'host:safety','edit_seq':index+1,'base_revision':head['file_revision'],'kind':'put','content_text':f'<h1 id="goal">Run update {index}</h1>','language':'html'}],'resolves':[]}
                receipt=suite.api('POST','/__persistence/host-mutate',{'channel':channel,'cell_id':cell,'operation':operation})
                run_receipts.append(receipt)
                assert receipt['outcome']=='accepted'
                checkpoints.append(suite.api('POST',f'/api/channels/{channel}/cells/{cell}/checkpoint'))
        elapsed=time.monotonic()-started
        assert elapsed>=10,'Sustained typing fixture did not run for ten seconds'
        expected='original '+prefix+''.join(str(index%10) for index in range(110))
        actual=await editor.input_value()
        assert actual==expected,f'Run/checkpoint activity replaced editor text: actual={actual!r}, expected={expected!r}, journal={await suite.journal(page,"notes.txt")!r}'
        frames=await frame.evaluate('window.__persistenceFrames')
        assert frames and all(item['connected'] for item in frames),'Concurrent activity replaced the editor node'
        selection=await editor.evaluate('el=>({start:el.selectionStart,end:el.selectionEnd})')
        assert selection['start']==selection['end']==len(expected),'Concurrent activity moved the typing caret'
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if suite.file(channel,cell,'notes.txt')==expected:break
            await page.wait_for_timeout(100)
        assert suite.file(channel,cell,'notes.txt')==expected,'Sustained current bytes did not reach the real server'
        assert len(run_receipts)==len(checkpoints)==3
        return {'typing_seconds':elapsed,'keystrokes':110,'host_run_writes':run_receipts,
                'run_activity_scope':'Synthetic run-owned document writes through the real host mutation/SQL/SSE pipeline; no generator manager is started.',
                'checkpoints':checkpoints,'same_editor_node':True,'end_caret':selection,'page_errors':errors}
    finally:await _close(suite,context,page,'safety-sustained-concurrent')


async def run_safety_cases(suite,browser):
    await suite.check('safety-renderer-switch-dirty-owner',lambda:renderer_switch(suite,browser))
    for role in ('native','pooled','standalone'):
        await suite.check('safety-run-primary-readonly-'+role,lambda role=role:run_primary_readonly(suite,browser,role))
    await suite.check('safety-sustained-run-checkpoint',lambda:sustained_with_run_writes(suite,browser))
