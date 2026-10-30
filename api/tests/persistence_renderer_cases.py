"""Real shipped-renderer adapters for the persistence torture suite.

These scenarios drive DOM controls and real file HTTP mutations. A synthetic
session socket enables Assistant's upload control without invoking a provider.
No renderer callbacks or file persistence APIs are mocked.
"""
from __future__ import annotations
import json
import sys
import time
import uuid


async def _close(suite,context,page,renderer):
    if sys.exc_info()[0] is not None:
        try:
            await page.screenshot(path=str(suite.directory/f'{renderer}-failure.png'),full_page=True)
            evidence=await page.evaluate('''()=>{const chunks=[];const visit=root=>{
                chunks.push(root.textContent);for(const node of root.querySelectorAll('*'))if(node.shadowRoot)visit(node.shadowRoot);
            };visit(document.body);return chunks.join('\\n').slice(-40000);}''')
            (suite.directory/f'{renderer}-failure.txt').write_text(evidence)
        except Exception:
            pass
    await context.close()


async def _find(page, selector, timeout=45):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        for frame in page.frames:
            for element in await frame.locator(selector).all():
                if await element.is_visible():
                    return frame,element
        await page.wait_for_timeout(100)
    raise TimeoutError(f'Visible shipped renderer control absent: {selector}')


async def _operations(page):
    return await page.evaluate('''()=>new Promise((resolve,reject)=>{
        const open=indexedDB.open('collimate-documents');open.onerror=()=>reject(open.error);
        open.onsuccess=()=>{const db=open.result;if(!db.objectStoreNames.contains('operations')){db.close();resolve([]);return;}
        const tx=db.transaction('operations','readonly'),read=tx.objectStore('operations').getAll();
        tx.oncomplete=()=>{resolve(read.result);db.close();};tx.onabort=()=>reject(tx.error);};
    })''')


async def _snapshot(suite,page,path):
    documents=await suite.journal(page,path)
    operations=await _operations(page)
    return {'documents':documents,'operations':[record for record in operations if any(m['path']==path for m in record['operation']['mutations'])]}


def _contains(snapshot,text):
    return any(text in record['body'] for record in snapshot['documents']) or any(text in json.dumps(record['operation']) for record in snapshot['operations'])


async def _document_case(suite,browser,renderer,filename,original,selector,mutate,visible):
    context,page,channel,cell,errors=await suite.fixture(browser,filename,original)
    try:
        frame,control=await _find(page,selector)
        suite.fault(refuse=True)
        token='persist-'+uuid.uuid4().hex[:8]
        await mutate(page,frame,control,token)
        await page.wait_for_timeout(400)
        snapshot=await _snapshot(suite,page,filename)
        assert _contains(snapshot,token),'Renderer did not durably report its real UI edit by the 0.4 s boundary'
        assert token not in json.dumps(suite.file(channel,cell,filename)),'Fault did not refuse the server write'
        await page.reload(wait_until='domcontentloaded')
        await suite.open_file(page,filename)
        frame,control=await _find(page,selector)
        assert await visible(page,frame,control,token),'Reload lost the visible shipped renderer edit'
        after=await _snapshot(suite,page,filename)
        assert _contains(after,token),'Reload lost the local durable snapshot'
        return {'renderer':renderer,'mechanism':'primary document control → immediate snapshot → browser journal',
                'boundary_seconds':.4,'server_refused':True,'durable_before_reload':snapshot,
                'durable_after_reload':after,'browser_errors':errors}
    finally:
        await _close(suite,context,page,renderer)


async def schema_form(suite,browser):
    async def mutate(_page,_frame,control,token):await control.fill(token)
    async def visible(_page,_frame,control,token):return await control.input_value()==token
    return await _document_case(suite,browser,'schema_form','settings.set_yaml',
        'fields:\n  - key: title\n    type: text\n    label: Title\nvalues:\n  title: original\n',
        'input[data-sf-control]',mutate,visible)


async def website(suite,browser):
    async def mutate(_page,_frame,control,token):
        await control.fill('https://example.invalid/'+token)
        await control.press('Enter')
    async def visible(_page,_frame,control,token):return token in await control.input_value()
    return await _document_case(suite,browser,'website','view.website',json.dumps({'url':'https://example.invalid/original'}),
        'input[placeholder="https://..."]',mutate,visible)


async def excalidraw(suite,browser):
    filename='drawing.excalidraw'
    original=json.dumps({'type':'excalidraw','version':2,'source':'persistence','elements':[],'appState':{},'files':{}})
    context,page,channel,cell,errors=await suite.fixture(browser,filename,original)
    try:
        frame,canvas=await _find(page,'.excalidraw canvas.interactive')
        suite.fault(refuse=True)
        # A real rectangle gesture changes the shipped Excalidraw scene.
        await frame.locator('.excalidraw').press('r')
        box=await canvas.bounding_box()
        assert box,'Excalidraw canvas has no geometry'
        await page.mouse.move(box['x']+120,box['y']+120)
        await page.mouse.down()
        await page.mouse.move(box['x']+260,box['y']+220,steps=8)
        await page.mouse.up()
        await page.wait_for_timeout(400)
        snapshot=await _snapshot(suite,page,filename)
        scenes=[json.loads(record['body']) for record in snapshot['documents']]
        assert scenes and any(scene.get('elements') for scene in scenes),'Drawing gesture has no durable scene snapshot at 0.4 s'
        expected=[element['id'] for element in scenes[-1]['elements']]
        server=suite.file(channel,cell,filename)
        assert not (json.loads(server) if isinstance(server,str) else server)['elements'],'Server refusal failed'
        await page.reload(wait_until='domcontentloaded')
        await suite.open_file(page,filename)
        recovered_frame,_=await _find(page,'.excalidraw canvas.interactive')
        recovered=await _snapshot(suite,page,filename)
        scene=json.loads(recovered['documents'][-1]['body'])
        assert [element['id'] for element in scene['elements']]==expected,'Reload lost the drawn elements'
        # Delete the restored visible shape through real keyboard selection.
        # A blank restored canvas cannot publish this element deletion.
        await recovered_frame.locator('.excalidraw').press('Control+a')
        await recovered_frame.locator('.excalidraw').press('Delete')
        await page.wait_for_timeout(400)
        deleted=await _snapshot(suite,page,filename)
        latest=json.loads(deleted['documents'][-1]['body'])
        assert all(element.get('isDeleted') for element in latest['elements'] if element['id'] in expected),'The restored shape was not present in the rendered canvas'
        return {'renderer':'excalidraw','mechanism':'canvas rectangle gesture → scene snapshot',
                'boundary_seconds':.4,'server_refused':True,'element_ids':expected,'browser_errors':errors}
    finally:await _close(suite,context,page,'excalidraw')


async def assistant(suite,browser):
    # Keep a session socket open so the real Assistant attachment control is
    # available. Its file upload and refused mutation still use the real app.
    context=await browser.new_context(viewport={'width':1440,'height':1000})
    sockets=[]
    async def socket(route):
        sockets.append(route)
        route.send(json.dumps({'t':'history','thread_id':'main','turn_id':'','pending_requests':[]}))
        route.on_message(lambda _message:None)
    await context.route_web_socket('**/persistence-chat*',socket)
    pointer=json.dumps({'chat_id':'persistence-chat','ws_path':'/persistence-chat','token':'test','active_thread':'main','threads':[]})
    context,page,channel,cell,errors=await suite.fixture(browser,'session.chat',pointer,context=context)
    path='attachments/probe.bin'
    payload=b'\x00\xffpersistence attachment\n'
    try:
        await page.get_by_role('button',name='Attach files',exact=True).wait_for()
        suite.fault(refuse=True)
        await page.locator('input[type=file]').last.set_input_files({'name':'probe.bin','mimeType':'application/octet-stream','buffer':payload})
        await page.wait_for_timeout(400)
        before=await _snapshot(suite,page,path)
        assert before['operations'],'Real Assistant attachment has no durable exact mutation record'
        mutation=before['operations'][-1]['operation']['mutations'][0]
        assert mutation.get('blob_hash') and mutation.get('encoding')=='binary','Attachment lost binary metadata'
        await page.reload(wait_until='domcontentloaded')
        await suite.open_file(page,'session.chat')
        await page.get_by_role('button',name='Attach files',exact=True).wait_for()
        after=await _snapshot(suite,page,path)
        assert after['operations'],'Reload lost the pending attachment mutation'
        assert after['operations'][-1]['operation']==before['operations'][-1]['operation'],'Recovery changed the uncertain binary operation'
        # The file contract preserves the submitted binary mutation. Restoring
        # an unsent chat message's composer/attachment selection is separate
        # session state, not another current-file authority.
        suite.fault()
        await page.reload(wait_until='domcontentloaded')
        await suite.open_file(page,'session.chat')
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            files=suite.api('GET',f'/api/channels/{channel}/cells/{cell}/files')['files']
            if any(file['path']==path and not file['deleted'] for file in files):break
            await page.wait_for_timeout(100)
        assert suite.file_bytes(channel,cell,path)==payload,'Recovered attachment bytes differ from the submitted binary'
        assert not (await _snapshot(suite,page,path))['operations'],'Accepted attachment kept its uncertain operation'
        return {'renderer':'assistant','mechanism':'defined attachment staging; synthetic open session, real admitted binary+file mutation',
                'boundary_seconds':.4,'server_refused':True,'exact_binary_recovery':True,
                'composer_scope':'Unsent chat message selection is outside the current-file persistence contract.',
                'operation':mutation,'browser_errors':errors}
    finally:await _close(suite,context,page,'assistant')


async def renderer_example(suite,browser):
    filename='preview.renderer_example'
    sample='sample.txt'
    source='''export default {mount:async(container,api)=>{const input=document.createElement('textarea');input.setAttribute('aria-label','Preview sample editor');input.value=await api.getContent();input.oninput=()=>api.reportChange(input.value);container.append(input);return()=>input.remove();}};'''
    context,page,channel,cell,errors=await suite.fixture(browser,filename,json.dumps({'renderer':'probe/probe.renderer','file':sample}),extra_files=[
        {'name':'probe/probe.renderer','content':'id: persistence_preview\nname: Persistence preview\nentrypoint: main.ts\nembed: iframe\nextensions: [.txt]\n'},
        {'name':'probe/main.ts','content':source},{'name':sample,'content':'original preview'}])
    try:
        checkbox=page.get_by_role('checkbox',name='Allow edits for this preview')
        await checkbox.wait_for()
        frame,control=await _find(page,'textarea[aria-label="Preview sample editor"]',90)
        await checkbox.check()
        suite.fault(refuse=True)
        token='preview-'+uuid.uuid4().hex[:8]
        await control.fill(token)
        await page.wait_for_timeout(400)
        before=await _snapshot(suite,page,sample)
        assert _contains(before,token),'Sealed preview edit has no durable browser snapshot'
        assert token not in suite.file(channel,cell,sample),'Server preview write refusal failed'
        await page.reload(wait_until='domcontentloaded')
        await suite.open_file(page,filename)
        _,control=await _find(page,'textarea[aria-label="Preview sample editor"]',90)
        assert token in await control.input_value(),'Reload lost visible sample edit in sealed preview'
        return {'renderer':'renderer_example','mechanism':'user-authorized sealed preview sample mutation; real compiler and sibling service',
                'boundary_seconds':.4,'server_refused':True,'durable_before_reload':before,'browser_errors':errors}
    finally:await _close(suite,context,page,'renderer_example')


async def run_renderer_cases(suite,browser):
    for name,case in [('schema_form',schema_form),('website',website),('excalidraw',excalidraw),('assistant',assistant),('renderer_example',renderer_example)]:
        await suite.check(f'editable-{name}-refused-reload-0.4',lambda case=case:case(suite,browser))
