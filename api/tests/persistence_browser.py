"""Permanent real-browser persistence torture suite.

Run from the app root after building frontend and re-vendoring the SDK:
  .venv/bin/python api/tests/persistence_browser.py --matrix full --output tmp/persistence-1008/torture-results.json

Owns a throwaway data directory and server. Faults are injected at the ASGI
boundary (including requests issued while a page unloads), never by mocking
renderer callbacks. The JSON receipt includes individual failures and frame
samples; any failed or uncovered editable renderer exits nonzero.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
from urllib.parse import parse_qs, quote, urlparse
import uuid
from persistence_renderer_cases import _close

ROOT = Path(__file__).resolve().parents[2]


def record_contains(record, text):
    """Inspect the exact durable representation, before derived text is ready."""
    recovery=record.get('recovery')
    if not recovery:return text in record.get('body','')
    def plain(node):
        if not isinstance(node,dict):return ''
        if node.get('type')=='text':return node.get('text','')
        if node.get('type')=='hardbreak':return '\n'
        return ''.join(plain(child) for child in node.get('content',[]))
    payload=json.loads(recovery['payload'])
    return text in plain(payload.get('doc',payload))


#: An empty tab's one control: it opens the cell's Files panel.
EMPTY_TAB_OPENER = 'Open existing or create a new file'


async def open_from_files_panel(page, path):
    """Open `path` from its cell's open Files panel: search for it, double-click
    its row (the one whose description starts with the path) for the file's
    options, and run their Open."""
    panel = page.get_by_role('dialog', name='Files')
    await panel.get_by_role('combobox', name='Find file').fill(path)
    await panel.get_by_text(re.compile('^' + re.escape(path) + ' · ')).dblclick()
    await panel.get_by_role('option').filter(has=page.get_by_text('Open', exact=True)).click()


def server(port: int, control: Path) -> None:
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    if os.environ.get('PERSISTENCE_TRACE_HANGS') == '1':
        faulthandler.dump_traceback_later(20, repeat=True)
    sys.path.insert(0, str(ROOT / 'api'))
    import uvicorn
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles
    from starlette.responses import JSONResponse, StreamingResponse
    measurements = None
    if os.environ.get('PERSISTENCE_MEASURE_SERVICE') == '1':
        from persistence_service_metrics import install
        measurements = install(control)
    import main
    main._INDEX_PATH = str(ROOT / 'frontend/dist/index.html')
    main.app.router.routes[:] = [r for r in main.app.router.routes if not isinstance(r, Mount) or r.name != 'static']
    main.app.mount('/', StaticFiles(directory=str(ROOT / 'frontend/dist'), html=True), name='static')

    async def instrumented(scope, receive, send):
        cfg = json.loads(control.read_text()) if control.exists() else {}
        path = scope.get('path', '')
        if scope['type'] == 'http' and path == '/__persistence/measurements' and measurements is not None:
            await JSONResponse(measurements)(scope, receive, send)
            return
        if scope['type'] == 'http' and scope.get('method') == 'POST' and path == '/__persistence/host-mutate':
            # Fault harness ingress only: execute an actual host mutation and
            # its normal SQL/memory/event publication, outside user ACLs.
            parts = []
            while True:
                part = await receive()
                parts.append(part.get('body', b''))
                if not part.get('more_body', False):
                    break
            payload = json.loads(b''.join(parts))
            from persistence import mutate_files
            result = await asyncio.to_thread(mutate_files, payload['channel'], payload['cell_id'],
                                             payload['operation'], actor='host')
            await JSONResponse(result)(scope, receive, send)
            return
        if scope['type'] == 'http' and path.endswith('/state/stream') and cfg.get('overflow'):
            async def events():
                yield b'event: overflow\ndata: {}\n\n'
            await StreamingResponse(events(), media_type='text/event-stream')(scope, receive, send)
            return
        if scope['type']=='http' and path.endswith('/state/stream'):
            closed=False
            async def stream_send(message):
                nonlocal closed
                if closed:return
                current=json.loads(control.read_text()) if control.exists() else {}
                if message['type']=='http.response.body' and current.get('overflow'):
                    closed=True
                    await send({'type':'http.response.body','body':b'event: overflow\ndata: {}\n\n','more_body':False})
                else:await send(message)
            await main.app(scope,receive,stream_send)
            return
        mutation = scope['type'] == 'http' and scope.get('method') == 'POST' and path.endswith('/files/mutate')
        if mutation:
            if cfg.get('lose_rename_response'):
                incoming=[]
                while True:
                    part=await receive()
                    incoming.append(part)
                    if not part.get('more_body',False):break
                payload=json.loads(b''.join(part.get('body',b'') for part in incoming))
                changes=payload.get('mutations',[])
                if len(changes)>1 and any(change['kind']=='delete' for change in changes):
                    cfg={**cfg,'lose_response':True}
                receive_original=receive
                async def replay_receive():
                    return incoming.pop(0) if incoming else await receive_original()
                receive=replay_receive
            if cfg.get('refuse'):
                await JSONResponse({'detail': 'injected storage unavailable'}, status_code=503)(scope, receive, send)
                return
            while cfg.get('hold_before'):
                await asyncio.sleep(.02)
                cfg = json.loads(control.read_text()) if control.exists() else {}
            await asyncio.sleep(cfg.get('delay_before', 0))
            sent = []
            async def capture(message):
                sent.append(message)
            await main.app(scope, receive, capture)
            if cfg.get('lose_response'):
                await JSONResponse({'detail': 'injected lost successful response'}, status_code=503)(scope, receive, send)
                return
            await asyncio.sleep(cfg.get('delay_after', 0))
            for message in sent:
                await send(message)
            return
        await main.app(scope, receive, send)
    uvicorn.run(instrumented, host='127.0.0.1', port=port, log_level='warning')


class Suite:
    def __init__(self, output: Path, only: list[str] | None = None):
        self.only = only
        self.output = output.resolve()
        self.directory = self.output.parent / ('browser-' + uuid.uuid4().hex[:8])
        self.directory.mkdir(parents=True)
        self.control = self.directory / 'faults.json'
        self.log = (self.directory / 'server.log').open('w')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.base = f'http://127.0.0.1:{self.port}'
        self.process = None
        self.rows = []
        self.manifest = []
        self.inventory = []
        self.notices = []

    def fault(self, **values):
        temporary = self.control.with_suffix('.tmp')
        temporary.write_text(json.dumps(values))
        temporary.replace(self.control)

    def api(self, method, path, body=None, *, timeout=30):
        request = urllib.request.Request(self.base + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={'content-type': 'application/json'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            try:
                return json.loads(raw)
            except ValueError:
                return raw

    def create_seeded_channel(self, label, *, timeout=180):
        """A new channel from the bundled Standard suggestion (its renderers
        open the probe files), with no tutorial cells. Answers the channel."""
        return self.api('POST', '/api/channels?name=' + quote(label),
                        {'suggested': {'id': 'standard', 'tutorial': False}}, timeout=timeout)

    def import_cell(self, channel, name, files):
        """One new cell in a new stack, made the way the app imports files:
        each ``{'name', 'content'}`` body streams to the CAS, then one import
        commit. Answers ``{'stack_ids', 'cell_ids'}``."""
        refs = []
        for item in files:
            upload = urllib.request.Request(self.base + '/api/blob/stream', method='POST',
                data=item['content'].encode(), headers={'content-type': 'application/octet-stream'})
            with urllib.request.urlopen(upload, timeout=30) as response:
                refs.append({'path': item['name'], 'blob_hash': json.loads(response.read())['blob_hash']})
        return self.api('POST', f'/api/channels/{channel}/import',
                        {'stacks': [{'name': '', 'cells': [{'name': name, 'files': refs, 'dirs': []}]}]})

    async def start(self):
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
            '--server', str(self.port), '--control', str(self.control)],
            cwd=self.directory, stdout=self.log, stderr=subprocess.STDOUT,
            env={**os.environ, 'PYTHON_KEYRING_BACKEND':'keyring.backends.null.Keyring',
                 'TMPDIR':str(self.directory), 'COLLIMATE_TOKEN':''})
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f'server failed; see {self.directory / "server.log"}')
            try:
                self.api('GET', '/api/state')
                return
            except Exception:
                await asyncio.sleep(.25)
        raise TimeoutError('isolated server did not become ready')

    async def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, 15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)

    async def fixture(self, browser, filename='notes.txt', body='original ', context=None, extra_files=None, existing=None):
        self.fault()
        if existing is None:
            label = 'persistence-' + uuid.uuid4().hex[:8]
            # Cold default seeding admits hundreds of bodies. Its setup latency
            # is separate from the measured editor/save/recovery boundaries.
            created = self.create_seeded_channel(label)
            channel = created['id']
            made = self.import_cell(channel, 'Persistence probe',
                                    [{'name':filename, 'content':body},*(extra_files or [])])
            made = {'cell_id': made['cell_ids'][0]}
        else:
            channel, cell_id = existing
            self.api('POST', f'/api/channels/{channel}/select')
            assert self.file(channel, cell_id, filename) == body
            made = {'cell_id': cell_id}
        context = context or await browser.new_context(viewport={'width':1440,'height':1000})
        await context.add_init_script("""(() => {
            window.__persistenceMetrics = {inputs: [], journals: [], longTasks: [], receivedInputs: [], snapshotInputs:{}};
            const metrics = window.__persistenceMetrics;
            metrics.stream={opens:0,overflows:0,errors:0};
            const NativeEventSource=window.EventSource;
            window.EventSource=class extends NativeEventSource {
                constructor(...args){super(...args);this.addEventListener('open',()=>metrics.stream.opens++);
                    this.addEventListener('overflow',()=>metrics.stream.overflows++);
                    this.addEventListener('error',()=>metrics.stream.errors++);}
            };
            window.addEventListener('message',event=>{
                if(event.origin===location.origin && event.data?.type==='PERSISTENCE_INPUT_METRIC')metrics.receivedInputs.push(event.data);
                if(event.origin===location.origin && event.data?.type==='PERSISTENCE_SNAPSHOT_METRIC')metrics.snapshotInputs[event.data.snapshotId]=event.data.at;
            });
            let lastKeyAt=null;
            const publishInputMetric=at=>{
                const input={type:'PERSISTENCE_INPUT_METRIC',at};
                if(window.parent!==window && location.origin!=='null')window.parent.postMessage(input,location.origin);
                else metrics.receivedInputs.push(input);
            };
            window.addEventListener('keydown',event=>{
                if(event.key.length===1&&!event.ctrlKey&&!event.metaKey&&!event.altKey){
                    lastKeyAt=performance.timeOrigin+performance.now();
                    window.__persistenceLastKeyAt=lastKeyAt;
                    // Transaction-driven editors may prevent native beforeinput.
                    // The physical key timestamp remains the durability origin.
                    publishInputMetric(lastKeyAt);
                }
            },true);
            window.addEventListener('input', () => {
                const started = performance.now();
                if(lastKeyAt===null)publishInputMetric(performance.timeOrigin+started);
                lastKeyAt=null;
                requestAnimationFrame(() => metrics.inputs.push({at: started, toFrame: performance.now()-started}));
            }, true);
            try {new PerformanceObserver(list => metrics.longTasks.push(...list.getEntries().map(x=>({at:x.startTime,duration:x.duration}))))
                .observe({type:'longtask',buffered:true});} catch {}
            const put = IDBObjectStore.prototype.put;
            IDBObjectStore.prototype.put = function(value, ...args) {
                if (this.name === 'documents' && value?.record) {
                    const started = performance.now();
                    const record = value.record;
                    const latestInput=metrics.receivedInputs.filter(input=>input.at<=performance.timeOrigin+started).at(-1);
                    const snapshotId=record.recovery?.snapshotId;
                    const inputAt=snapshotId?metrics.snapshotInputs[snapshotId]:latestInput?.at;
                    this.transaction.addEventListener('complete', () => metrics.journals.push({
                        started, completed:performance.now(), transactionMs:performance.now()-started,
                        inputToCommitMs:inputAt!==undefined?performance.timeOrigin+performance.now()-inputAt:null,
                        inputCorrelation:snapshotId?'exact-snapshot-id':'latest-input-at-put',
                        path:record.identity.path, sequence:record.editSeq, bytes:record.body?.length??0,
                        recoverySnapshotId:record.recovery?.snapshotId??null}));
                }
                return put.call(this,value,...args);
            };
        })();""")
        async def measured_renderer(route):
            response=await route.fetch()
            code=await response.text()
            # Time the shipped serializer without changing its return value or
            # scheduling. Keep this instrumentation in the test proxy only.
            code=re.sub(r'\b([A-Za-z_$][\w$]*)\.getMarkdown\(\)',
                r'globalThis.__measureMarkdown(() => \1.getMarkdown())',code)
            prefix="globalThis.__measureMarkdown = callback => {const started=performance.now();try{return callback();}finally{(globalThis.__persistencePluginTimes ??= []).push({kind:'getMarkdown',started,duration:performance.now()-started});}};\n"
            # Anchored authored statements cannot match the generated codec's
            # quoted worker source: its original bytes/hash must stay intact.
            code=re.sub(r'^(\s*)payload: JSON\.stringify\(([A-Za-z_$][\w$]*)\.toJSON\(\)\)(,?)$',
                r"\1payload: globalThis.__measureSnapshot('toJSON', () => JSON.stringify(\2.toJSON()))\3",code,flags=re.M)
            code=re.sub(r'^(\s*)([A-Za-z_$][\w$]*)\.saveSnapshot\(([A-Za-z_$][\w$]*)\);$',
                r"\1globalThis.__measureSnapshot('saveSnapshot', () => \2.saveSnapshot(\3), \3.snapshotId);",code,flags=re.M)
            prefix+="globalThis.__measureSnapshot = (kind,callback,snapshotId) => {const started=performance.now();if(snapshotId){const metric={type:'PERSISTENCE_SNAPSHOT_METRIC',snapshotId,at:globalThis.__persistenceLastKeyAt??performance.timeOrigin+started};if(parent!==window&&location.origin!=='null')parent.postMessage(metric,location.origin);else if(globalThis.__persistenceMetrics)globalThis.__persistenceMetrics.snapshotInputs[snapshotId]=metric.at;}try{return callback();}finally{(globalThis.__persistencePluginTimes ??= []).push({kind,started,duration:performance.now()-started});}};\n"
            await route.fulfill(response=response,body=prefix+code)
        await context.route('**/api/renderers/*/source*',measured_renderer)
        page = await context.new_page()
        page._persistence_target = {'filename': filename, 'cell': made['cell_id']}
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(self.base, wait_until='domcontentloaded')
        await self.open_file(page, filename)
        return context, page, channel, made['cell_id'], errors

    async def open_file(self, page, filename):
        if hasattr(page, '_persistence_target'):
            page._persistence_target['filename'] = filename
        # Recovery of an outstanding structural write may report the injected
        # server outage. Acknowledge that visible notice, retaining its text in
        # the receipt, before inspecting the recovered renderer itself.
        for _attempt in range(4):
            notice=page.get_by_role('dialog').filter(has=page.get_by_role('heading',name='File recovery needs attention',exact=True))
            if await notice.count() and await notice.is_visible():
                self.notices.append(await notice.inner_text())
                await notice.get_by_role('button',name='OK',exact=True).click()
            try:
                await page.locator('button[aria-label="Persistence probe"]').last.click(timeout=1000)
                break
            except Exception:
                if _attempt==3:raise
        await page.wait_for_timeout(250)
        target = getattr(page, '_persistence_target', {})
        host = page.locator(f'[data-cell-id="{target["cell"]}"]').last if target.get('cell') else page
        opened = host.locator(f'[data-file-path="{filename}"]')
        if not (await opened.count() and await opened.last.is_visible()):
            tab = host.locator(f'[role="tab"][title="{filename}"]')
            if await tab.count() and await tab.last.is_visible():
                await tab.last.click()
            else:
                opener = host.get_by_role('button', name=EMPTY_TAB_OPENER, exact=True)
                if not (await opener.count() and await opener.last.is_visible()):
                    await host.get_by_role('button', name='New tab', exact=True).last.click()
                await opener.last.click()
                await open_from_files_panel(page, filename)
            await opened.last.wait_for(state='visible')
        await page.wait_for_timeout(150)
        focus=page.get_by_title('Click to focus',exact=True)
        if await focus.count() and await focus.last.is_visible():await focus.last.click()

    async def editor(self, page, kind='text', filename=None):
        target = getattr(page, '_persistence_target', {})
        filename = filename or target.get('filename')
        cell = target.get('cell') if filename == target.get('filename') else None
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            for frame in page.frames:
                if kind in ('milkdown', 'monaco'):
                    coordinates = parse_qs(urlparse(frame.url).query)
                    if filename is not None and coordinates.get('filename') != [filename]:
                        continue
                    if cell is not None and coordinates.get('cellId') != [cell]:
                        continue
                    # A parked iframe retains its live inner editor. Inner
                    # DOM visibility cannot establish that its physical view
                    # is the current tab: the pool hides and moves the frame.
                    physical = await frame.frame_element()
                    if not await physical.evaluate('''element => {
                        const rect = element.getBoundingClientRect();
                        return element.checkVisibility({checkVisibilityCSS:true})
                            && rect.width > 0 && rect.height > 0
                            && rect.right > 0 && rect.bottom > 0
                            && rect.left < innerWidth && rect.top < innerHeight;
                    }'''):
                        continue
                selector = '.ProseMirror[contenteditable="true"]' if kind == 'milkdown' else '.monaco-editor textarea' if kind == 'monaco' else 'textarea'
                scope = frame.locator(f'[data-cell-id="{cell}"]') if cell and kind not in ('milkdown','monaco') and frame == page.main_frame else frame
                if filename and kind == 'text' and frame == page.main_frame:
                    scope = scope.locator(f'[data-file-path="{filename}"]')
                for element in await scope.locator(selector).all():
                    if await element.is_visible():
                        return frame, element
            await page.wait_for_timeout(100)
        raise TimeoutError(f'{kind} editor did not become available')

    async def record_frames(self, editor):
        await editor.evaluate('''element => {
            window.__persistenceFrames=[];window.__persistenceObserved=element;
            const sample=()=>{
                const node=window.__persistenceObserved;
                window.__persistenceFrames.push({at:performance.now(),connected:node.isConnected,
                    length:node.value?.length??node.textContent.length,selection:node.selectionStart??null,
                    scrollTop:node.scrollTop});
                if(window.__persistenceFrames.length<2000)requestAnimationFrame(sample);
            };requestAnimationFrame(sample);
        }''')

    async def journal(self, page, path):
        return await page.evaluate('''path=>new Promise((resolve,reject)=>{
            const open=indexedDB.open('collimate-documents');
            open.onerror=()=>reject(open.error);
            open.onsuccess=()=>{const db=open.result;const tx=db.transaction('documents','readonly');
                const request=tx.objectStore('documents').getAll();
                tx.oncomplete=()=>{resolve(request.result.map(x=>x.record).filter(x=>x.identity.path===path));db.close();};
                tx.onabort=()=>reject(tx.error);};
        })''', path)

    async def type_suffix(self, page, kind='text', suffix=None):
        frame, editor = await self.editor(page, kind)
        await editor.focus()
        await editor.press('Control+End')
        await self.record_frames(editor)
        suffix = suffix or ' typed-' + uuid.uuid4().hex[:8]
        try:
            await editor.press_sequentially(suffix, delay=35)
        except Exception:
            measurements=await frame.evaluate('({metrics:window.__persistenceMetrics,plugins:window.__persistencePluginTimes})')
            measurements['browser_journal']=await page.evaluate('window.__persistenceMetrics')
            (self.directory/f'typing-failure-{kind}.json').write_text(json.dumps(measurements,indent=2))
            raise
        return frame, editor, suffix

    def file(self, channel, cell, path):
        return self.api('GET', f'/api/channels/{channel}/cells/{cell}/files/{path}')

    def file_bytes(self, channel, cell, path):
        with urllib.request.urlopen(self.base+f'/api/channels/{channel}/cells/{cell}/files/{path}',timeout=30) as response:
            return response.read()

    async def check(self, name, task, *, gate='P2'):
        self.manifest.append({'scenario': name, 'gate': gate,
            'baseline': 'archived-original' if re.fullmatch(r'reload-(0\.4|1\.6|2\.6|5\.1)-(True|False)', name)
                        else 'first-run-after-implementation; original baseline unavailable'})
        if self.only and not any(token in name for token in self.only):
            return
        started = time.monotonic()
        evidence = {}
        print(json.dumps({'scenario':name, 'status':'running'}), flush=True)
        try:
            async with asyncio.timeout(240):
                evidence = await task() or {}
            runtime_errors = evidence.get('page_errors', evidence.get('browser_errors', []))
            assert not runtime_errors, f'Uncaught browser errors: {runtime_errors}'
            row = {'scenario':name, 'status':'passed', **(evidence or {})}
        except Exception as error:
            row = {'scenario':name, 'status':'failed', **evidence, 'error':repr(error),'traceback':traceback.format_exc()}
        if self.notices:
            row['recovery_notices']=self.notices[:]
            self.notices.clear()
        row['seconds'] = round(time.monotonic() - started, 3)
        self.rows.append(row)
        print(json.dumps(row), flush=True)
        self.output.write_text(json.dumps({'scenarios':self.rows,'scenario_manifest':self.manifest,'renderer_inventory':self.inventory,
            'server_log':str(self.directory/'server.log')}, indent=2))
        self.fault()

    async def reload(self, browser, delay, refused, large=False, kind='text', fullscreen=False, original=None):
        filename = {'milkdown':'notes.md','monaco':'notes.py','mermaid':'notes.mmd','slides':'notes.slides'}.get(kind,'notes.txt')
        if original is None:original = ('line\n' * 15000 if large else 'graph TD\n A --> B\n' if kind=='mermaid' else '# Original slide\n' if kind=='slides' else 'original ')
        context, page, channel, cell, errors = await self.fixture(browser,filename,original)
        try:
            if fullscreen:
                # Expansion hands the existing renderer lease off. Opening
                # it while the initial placeholder is loading exercises no
                # standalone editor and can leave the inline view selected.
                await self.editor(page,kind)
                await page.get_by_role('button',name='Expand the cell view to the full window').last.click()
                iframe=page.locator('[data-standalone-renderer] iframe').last
                await iframe.wait_for(state='visible')
                node=await iframe.element_handle()
                standalone_frame=await node.content_frame()
                selector='.ProseMirror[contenteditable="true"]' if kind=='milkdown' else '.monaco-editor textarea'
                standalone_editor=standalone_frame.locator(selector).first
                await standalone_editor.wait_for(state='visible')
            if kind=='slides':await page.get_by_role('button',name='edit source',exact=True).click()
            self.fault(refuse=refused)
            if fullscreen:
                frame,editor=standalone_frame,standalone_editor
                frame_node=await frame.frame_element()
                assert await frame_node.evaluate("element=>!!element.closest('[data-standalone-renderer]')"),'Fullscreen probe selected an inline renderer'
                await editor.focus()
                await editor.press('Control+End')
                await self.record_frames(editor)
                suffix=' typed-'+uuid.uuid4().hex[:8]
                await editor.press_sequentially(suffix,delay=35)
            else:
                frame, editor, suffix = await self.type_suffix(page,kind)
            await page.wait_for_timeout(delay * 1000)
            records = await self.journal(page,filename)
            before = self.file(channel,cell,filename)
            frames = await frame.evaluate('window.__persistenceFrames')
            measurements=await frame.evaluate('({metrics:window.__persistenceMetrics,plugins:window.__persistencePluginTimes})')
            measurements['host']=await page.evaluate('window.__persistenceMetrics')
            # Capture the observed boundary before asserting, so a failure
            # retains the exact journal, accepted bytes and physical host.
            # Collecting these diagnostics does not reread or extend the
            # captured delay's local durability observation.
            boundary={'delay':delay,'refused':refused,'renderer':kind,'fullscreen':fullscreen,
                      'channel':channel,'cell':cell,'filename':filename,'suffix':suffix,'frame_url':frame.url,
                      'local_records':records,'accepted_body':before,'frames':frames,'metrics':measurements,
                      'visible_body':await editor.evaluate("el=>el.closest('.monaco-editor')?.textContent??el.value??el.textContent"),
                      'page_errors':errors}
            (self.directory/f'reload-boundary-{kind}-{uuid.uuid4().hex[:8]}.json').write_text(json.dumps(boundary,indent=2))
            assert any(record_contains(item,suffix) for item in records) or (not refused and suffix in before), 'latest typed snapshot is neither locally durable nor server accepted'
            assert all(item['connected'] for item in frames), 'editor node was replaced during typing'
            await page.reload(wait_until='domcontentloaded')
            await self.open_file(page,filename)
            if kind=='slides':await page.get_by_role('button',name='edit source',exact=True).click()
            await self.editor(page,kind)
            await page.wait_for_timeout(400)
            records_after = await self.journal(page,filename)
            _, recovered = await self.editor(page,kind)
            await recovered.focus()
            await recovered.press('Control+End')
            await page.wait_for_timeout(100)
            visible = await recovered.input_value() if kind not in ('milkdown','monaco') else await recovered.evaluate("el => el.closest('.monaco-editor')?.textContent ?? el.textContent")
            assert suffix.strip() in visible.replace('\u00a0',' '), 'reload lost the visible editor text'
            assert any(record_contains(item,suffix) for item in records_after) or (not refused and suffix in self.file(channel,cell,filename)), 'reload lost the durable snapshot'
            for prior in records:
                if prior.get('recovery'):
                    recovered_record=next((item for item in records_after if item['writerId']==prior['writerId']),None)
                    assert recovered_record and recovered_record.get('recovery',{}).get('payload')==prior['recovery']['payload'],'Reload changed the exact editor recovery snapshot'
            if refused:
                assert suffix not in before, 'fault failed to refuse server writes'
                self.fault()
                retries=page.get_by_role('button',name='Retry save')
                if await retries.count():
                    await retries.first.click()
            else:
                assert suffix in self.file(channel,cell,filename), 'accepted server bytes lost the edit'
            return {'delay':delay,'refused':refused,'large':large,'renderer':kind,'host':'standalone' if fullscreen else 'pooled' if kind in ('milkdown','monaco') else 'native',
                    'initial_bytes':len(original.encode()),'initial_characters':len(original),
                    'local_records':len(records_after),'frames':len(frames),'frame_sample':frames[::max(1,len(frames)//12)],'page_errors':errors,'metrics':measurements}
        finally:
            await context.close()

    async def slow_and_concurrent(self, browser, fault):
        context,page,channel,cell,errors=await self.fixture(browser)
        try:
            self.fault(**fault)
            frame,editor,suffix=await self.type_suffix(page,suffix=' sustained-')
            for index in range(100):
                await editor.press_sequentially(str(index%10),delay=100)
                if index in (15,45,75):
                    self.api('POST',f'/api/channels/{channel}/cells/{cell}/checkpoint')
            records=await self.journal(page,'notes.txt')
            expected='sustained-'+''.join(str(i%10) for i in range(100))
            assert any(expected in record['body'] for record in records), 'Sustained latest text is missing from the browser journal'
            frames=await frame.evaluate('window.__persistenceFrames')
            assert all(item['connected'] for item in frames)
            self.fault()
            await page.wait_for_timeout(11000 if fault.get('delay_before') else 800)
            retries=page.get_by_role('button',name='Retry save')
            if await retries.count():await retries.first.click()
            await page.wait_for_timeout(1000)
            accepted=self.file(channel,cell,'notes.txt')
            assert expected in accepted, f'Latest sustained text did not reach the server after recovery: {accepted!r}'
            return {'fault':fault,'frames':len(frames),'page_errors':errors}
        finally:await context.close()

    async def conflict(self,browser):
        context,page,channel,cell,errors=await self.fixture(browser)
        try:
            self.fault(refuse=True)
            _,editor,suffix=await self.type_suffix(page)
            await page.wait_for_timeout(300)
            self.fault()
            inventory=self.api('GET',f'/api/channels/{channel}/cells/{cell}/files')['files']
            current=next(file for file in inventory if file['path']=='notes.txt')
            result=self.api('POST',f'/api/channels/{channel}/cells/{cell}/files/mutate',{
                'operation_id':uuid.uuid4().hex,'mutations':[{'path':'notes.txt','writer_id':'external-run','edit_seq':1,'base_revision':current['file_revision'],'kind':'put','content_text':'competing run body'}],'resolves':[]})
            assert result['outcome']=='accepted'
            await page.get_by_role('button',name='Review versions',exact=True).first.wait_for(timeout=10000)
            assert suffix in await editor.input_value(), 'competing server revision replaced dirty editor'
            await self.stop()
            await self.start()
            await page.reload(wait_until='domcontentloaded')
            await self.open_file(page,'notes.txt')
            await page.get_by_role('button',name='Review versions',exact=True).first.wait_for(timeout=15000)
            await page.get_by_role('button',name='Review versions',exact=True).first.click()
            await page.get_by_role('button',name='Keep my edits',exact=True).click()
            await page.wait_for_timeout(800)
            assert suffix in self.file(channel,cell,'notes.txt')
            return {'server_restart':True,'explicit_resolution':True,'page_errors':errors}
        finally:await context.close()

    async def two_windows(self,browser):
        context,page,channel,cell,errors=await self.fixture(browser)
        try:
            other=await context.new_page()
            other._persistence_target={'filename':'notes.txt','cell':cell}
            other.on('pageerror',lambda error:errors.append('second window: '+str(error)))
            await other.goto(self.base,wait_until='domcontentloaded')
            await self.open_file(other,'notes.txt')
            self.fault(refuse=True)
            _,first,left=await self.type_suffix(page,suffix=' window-one')
            _,second,right=await self.type_suffix(other,suffix=' window-two')
            await other.wait_for_timeout(400)
            records=await self.journal(other,'notes.txt')
            selected=[]
            for window,control in [(page,first),(other,second)]:
                selected.append(await control.evaluate('''element=>{
                    const closest=selector=>{let node=element;while(node){const found=node.closest?.(selector);if(found)return found;node=node.getRootNode()?.host;}return null;};
                    return {cell:closest('[data-cell-id]')?.dataset.cellId,
                        path:closest('[data-file-path]')?.dataset.filePath,
                        value:element.value,readOnly:element.readOnly,disabled:element.disabled,
                        writer:sessionStorage.getItem('collimate.documentWriter')};
                }'''))
            boundary={'channel':channel,'cell':cell,'expected':[left,right],'records':records,
                      'selected_hosts':selected,'page_errors':errors,
                      'metrics':[await window.evaluate('window.__persistenceMetrics') for window in [page,other]]}
            evidence_path=self.directory/f'two-windows-{cell}-boundary.json'
            evidence_path.write_text(json.dumps(boundary,indent=2))
            assert any(left in r['body'] for r in records) and any(right in r['body'] for r in records), f'Two-window local recovery missing; see {evidence_path}'
            assert len({r['writerId'] for r in records})==2
            self.fault()
            await page.get_by_role('button',name='Retry save',exact=True).click()
            await other.get_by_role('button',name='Review versions',exact=True).first.wait_for(timeout=10000)
            assert right in await second.input_value() and left in await first.input_value()
            await other.reload(wait_until='domcontentloaded')
            await self.open_file(other,'notes.txt')
            _,recovered=await self.editor(other)
            assert right in await recovered.input_value()
            await other.get_by_role('button',name='Review versions',exact=True).first.click()
            await other.get_by_role('button',name='Keep my edits',exact=True).click()
            await other.wait_for_timeout(500)
            assert right in self.file(channel,cell,'notes.txt')
            return {'renderer':'text','writers':2,'separate_recovery_records':True,'explicit_resolution':True,'page_errors':errors}
        finally:await context.close()

    async def local_storage_failure(self,browser):
        context,page,channel,cell,errors=await self.fixture(browser)
        try:
            await page.evaluate("""() => {
                window.__refuseDraftStorage=true;
                const put=IDBObjectStore.prototype.put;
                IDBObjectStore.prototype.put=function(...args) {
                    const result=put.apply(this,args);
                    if(this.name==='documents' && window.__refuseDraftStorage)this.transaction.abort();
                    return result;
                };
            }""")
            _,editor,suffix=await self.type_suffix(page)
            await page.get_by_text('Local save failed',exact=True).wait_for(timeout=10000)
            assert suffix in await editor.input_value()
            assert suffix not in self.file(channel,cell,'notes.txt')
            assert not await self.journal(page,'notes.txt')
            await page.evaluate('window.__refuseDraftStorage=false')
            await page.get_by_role('button',name='Retry save',exact=True).click()
            await page.wait_for_timeout(500)
            assert suffix in self.file(channel,cell,'notes.txt')
            return {'renderer':'text','failed_local_transaction_visible':True,'retry_preserved_bytes':True,'page_errors':errors}
        finally:await context.close()

    def mutate(self,channel,cell,path,body,writer='external-probe'):
        inventory=self.api('GET',f'/api/channels/{channel}/cells/{cell}/files')['files']
        base=next((file['file_revision'] for file in inventory if file['path']==path),0)
        return self.api('POST',f'/api/channels/{channel}/cells/{cell}/files/mutate',{
            'operation_id':uuid.uuid4().hex,'mutations':[{'path':path,'writer_id':writer+'-'+uuid.uuid4().hex,
            'edit_seq':1,'base_revision':base,'kind':'put','content_text':body}],'resolves':[]})

    async def different_windows(self,browser):
        context,page,channel,cell,errors=await self.fixture(browser,extra_files=[{'name':'other.txt','content':'other original '}])
        try:
            other=await context.new_page()
            other._persistence_target={'filename':'other.txt','cell':cell}
            other.on('pageerror',lambda error:errors.append('second window: '+str(error)))
            await other.goto(self.base,wait_until='domcontentloaded')
            await other.get_by_role('button',name='Show files',exact=True).last.click()
            await other.get_by_role('complementary',name='Files',exact=True).get_by_text('other.txt',exact=True).dblclick()
            _,_,left=await self.type_suffix(page,suffix=' first-file')
            _,_,right=await self.type_suffix(other,suffix=' second-file')
            await page.wait_for_timeout(700)
            assert left in self.file(channel,cell,'notes.txt')
            assert right in self.file(channel,cell,'other.txt')
            assert not await page.get_by_role('button',name='Review versions',exact=True).count()
            assert not await other.get_by_role('button',name='Review versions',exact=True).count()
            return {'renderer':'text','writers':2,'different_paths':True,'page_errors':errors}
        finally:await context.close()

    async def sse_overflow(self,browser):
        context,page,channel,cell,errors=await self.fixture(browser,extra_files=[{'name':'other.txt','content':'original sibling'}])
        try:
            self.fault(refuse=True)
            _,editor,suffix=await self.type_suffix(page)
            await page.wait_for_timeout(400)
            await context.set_offline(True)
            await page.wait_for_timeout(100)
            self.fault(overflow=True)
            self.mutate(channel,cell,'other.txt','sibling while offline')
            await context.set_offline(False)
            await page.wait_for_timeout(500)
            self.fault(refuse=True)
            await page.wait_for_timeout(1000)
            assert suffix in await editor.input_value(),'SSE overflow/resync replaced the dirty editor'
            assert any(suffix in record['body'] for record in await self.journal(page,'notes.txt'))
            assert self.file(channel,cell,'other.txt')=='sibling while offline'
            stream=await page.evaluate('window.__persistenceMetrics.stream')
            assert stream['overflows']>0,f'The injection did not deliver an SSE overflow: {stream}'
            self.fault()
            retry=page.get_by_role('button',name='Retry save',exact=True)
            if await retry.count():await retry.first.click()
            await page.wait_for_timeout(700)
            assert suffix in self.file(channel,cell,'notes.txt')
            return {'renderer':'text','overflow_and_reconnect':True,'stream':stream,'dirty_body_preserved':True,'page_errors':errors}
        finally:await context.close()

    async def overtaken_read(self,browser):
        context,page,channel,cell,errors=await self.fixture(browser)
        started=asyncio.Event()
        release=asyncio.Event()
        async def held_read(route):
            started.set()
            await release.wait()
            await route.continue_()
        try:
            await page.route('**/files/notes.txt?revision=*',held_read)
            self.mutate(channel,cell,'notes.txt','new server content')
            await asyncio.wait_for(started.wait(),10)
            self.fault(refuse=True)
            _,editor,suffix=await self.type_suffix(page)
            release.set()
            await page.wait_for_timeout(500)
            assert suffix in await editor.input_value(),'An older in-flight clean read replaced new typing'
            assert any(suffix in record['body'] for record in await self.journal(page,'notes.txt'))
            self.fault()
            await page.get_by_role('button',name='Retry save',exact=True).click()
            await page.get_by_role('button',name='Review versions',exact=True).first.wait_for(timeout=10000)
            assert suffix in await editor.input_value()
            return {'renderer':'text','clean_read_overtaken_by_edit':True,'candidate_preserved':True,'page_errors':errors}
        finally:
            release.set()
            await context.close()

    async def pool_lifetime(self,browser):
        context,page,channel,cell,errors=await self.fixture(browser,'notes.md','original ')
        try:
            for index in range(22):
                self.import_cell(channel,f'Pool neighbor {index:02}',
                    [{'name':f'neighbor-{index:02}.md','content':f'# Neighbor {index}'}])
            await page.reload(wait_until='domcontentloaded')
            await self.open_file(page,'notes.md')
            self.fault(refuse=True)
            frame,editor,suffix=await self.type_suffix(page,'milkdown')
            original_frame=await frame.frame_element()
            await original_frame.evaluate("element=>element.dataset.persistenceProbe='original'")
            await page.wait_for_timeout(400)
            async def visit(index):
                await page.locator(f'button[aria-label="Pool neighbor {index:02}"]').click()
                await page.wait_for_timeout(350)
                opener=page.get_by_role('button',name=EMPTY_TAB_OPENER,exact=True)
                for button in await opener.all():
                    if await button.is_visible():
                        await button.click()
                        break
                await open_from_files_panel(page,f'neighbor-{index:02}.md')
                await self.editor(page,'milkdown',f'neighbor-{index:02}.md')
                await page.wait_for_timeout(350)
            await visit(0)
            assert await original_frame.evaluate('element=>element.isConnected'),'Warm park discarded the existing frame'
            await self.open_file(page,'notes.md')
            same_frame,recovered=await self.editor(page,'milkdown','notes.md')
            assert await (await same_frame.frame_element()).get_attribute('data-persistence-probe')=='original'
            assert suffix.strip() in await recovered.inner_text(),'Warm park lost current dirty text'
            for index in range(1,22):await visit(index)
            assert not await original_frame.evaluate('element=>element.isConnected'),'The fixture did not exceed the real parked-frame LRU capacity'
            await self.open_file(page,'notes.md')
            new_frame,recovered=await self.editor(page,'milkdown','notes.md')
            assert await (await new_frame.frame_element()).get_attribute('data-persistence-probe') is None
            assert suffix.strip() in await recovered.inner_text(),'Evicted view re-created stale or empty text'
            assert any(record_contains(record,suffix) for record in await self.journal(page,'notes.md'))
            return {'renderer':'milkdown','warm_park_same_frame':True,'real_lru_eviction':True,
                'recreated_from_latest_durable_owner':True,'neighbors':22,'page_errors':errors}
        finally:await _close(self,context,page,'pool-lifetime')

    async def dirty_rename(self,browser,lost_response=False):
        context,page,channel,cell,errors=await self.fixture(browser)
        try:
            await page.get_by_role('button',name='Show files',exact=True).last.click()
            self.fault(hold_before=True,lose_rename_response=lost_response)
            _,editor,suffix=await self.type_suffix(page,suffix=' rename-draft')
            async def enter_name():
                row=page.locator('[data-filenode-path="notes.txt"]')
                await row.hover()
                await row.get_by_title('Rename',exact=True).click()
                field=row.locator('input')
                await field.fill('renamed.txt')
                await field.press('Enter')
            await enter_name()
            dialog=page.get_by_role('dialog').filter(has=page.get_by_role('heading',name='Save your edits and rename?',exact=True))
            await dialog.get_by_role('button',name='Cancel',exact=True).click()
            assert suffix in await editor.input_value(),'Cancel changed the dirty document'
            assert not any(file['path']=='renamed.txt' for file in self.api('GET',f'/api/channels/{channel}/cells/{cell}/files')['files'])
            # A new edit remains dirty while the earlier save is in flight.
            await editor.focus()
            await editor.press('Control+End')
            await editor.press_sequentially(' after-cancel',delay=10)
            expected='original '+suffix+' after-cancel'
            await enter_name()
            await dialog.get_by_role('button',name='Save and rename',exact=True).click()
            self.fault(lose_rename_response=lost_response)
            await page.wait_for_timeout(3500)
            if lost_response:
                operations=await page.evaluate('''()=>new Promise((resolve,reject)=>{
                    const open=indexedDB.open('collimate-documents');open.onerror=()=>reject(open.error);
                    open.onsuccess=()=>{const db=open.result,tx=db.transaction('operations','readonly'),read=tx.objectStore('operations').getAll();
                        tx.oncomplete=()=>{resolve(read.result);db.close()};tx.onabort=()=>reject(tx.error)};
                })''')
                assert any(record.get('renames') for record in operations),'Lost rename receipt discarded its exact durable operation'
                self.fault()
                await page.reload(wait_until='domcontentloaded')
                await self.open_file(page,'renamed.txt')
            else:self.fault()
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                inventory=self.api('GET',f'/api/channels/{channel}/cells/{cell}/files')['files']
                source=next(file for file in inventory if file['path']=='notes.txt')
                target=next((file for file in inventory if file['path']=='renamed.txt'),None)
                if source['deleted'] and target:break
                await page.wait_for_timeout(100)
            assert source['deleted'] and target and not target['deleted']
            assert self.file(channel,cell,'renamed.txt')==expected,'Save and rename did not use the captured current editor bytes'
            await self.open_file(page,'renamed.txt')
            _,renamed=await self.editor(page)
            assert await renamed.input_value()==expected,'Renamed view did not follow the accepted identity'
            return {'renderer':'text','cancel_preserved_buffer':True,'atomic_dirty_rename':True,
                    'lost_response_recovered':lost_response,'source_tombstone':source['file_revision'],
                    'target_revision':target['file_revision'],'page_errors':errors}
        finally:await _close(self,context,page,'dirty-rename')

    async def close_reopen(self,browser,kind='text'):
        filename='notes.md' if kind=='milkdown' else 'notes.txt'
        context,page,channel,cell,errors=await self.fixture(browser,filename,'original ')
        try:
            self.fault(refuse=True)
            _,_,suffix=await self.type_suffix(page,kind)
            await page.wait_for_timeout(400)
            prior=await self.journal(page,filename)
            assert any(record_contains(record,suffix) for record in prior),'Close has no locally durable latest snapshot at400ms'
            close=page.get_by_role('button',name=f'Close {filename}',exact=True)
            await close.focus()
            await close.press('Enter')
            await page.get_by_role('button',name=EMPTY_TAB_OPENER,exact=True).last.wait_for(timeout=5000)
            await self.open_file(page,filename)
            deadline=time.monotonic()+5
            observations=[]
            while True:
                frame,editor=await self.editor(page,kind)
                visible=await editor.inner_text() if kind=='milkdown' else await editor.input_value()
                observations.append({'at':time.monotonic(),'body':visible,'frame_url':frame.url})
                if suffix.strip() in visible or time.monotonic()>=deadline:break
                await page.wait_for_timeout(100)
            after=await self.journal(page,filename)
            evidence={'renderer':kind,'suffix':suffix,'prior_records':prior,'reopened_records':after,
                      'observations':observations,'accepted_body':self.file(channel,cell,filename),'page_errors':errors}
            (self.directory/f'close-reopen-boundary-{kind}.json').write_text(json.dumps(evidence,indent=2))
            assert suffix.strip() in visible,'Closing and re-leasing a view lost the dirty owner'
            assert any(record_contains(record,suffix) for record in after)
            assert suffix not in self.file(channel,cell,filename)
            return {'renderer':kind,'view_closed_while_server_refused':True,'reopened_latest_owner':True,'page_errors':errors}
        finally:await _close(self,context,page,'close-reopen-'+kind)

    async def renderer_inventory(self):
        import yaml
        for manifest in sorted((ROOT/'content/renderers').rglob('*.renderer')):
            config=yaml.safe_load(manifest.read_text())
            source='\n'.join(path.read_text() for path in manifest.parent.glob('*.ts*') if '.test.' not in path.name)
            editable=any(token in source for token in ('saveContent','saveSnapshot','api.reportChange(', 'stageDraftFile','saveSiblingFile'))
            self.inventory.append({'id':config['id'],'embed':config.get('embed'),'editable':editable,
                'manifest':str(manifest.relative_to(ROOT)), 'status':'pending' if editable else 'not-applicable',
                'reason':None if editable else 'No document or sidecar mutation UI in the shipped renderer source.'})


async def run(args):
    from playwright.async_api import async_playwright
    from persistence_renderer_cases import run_renderer_cases
    from persistence_ordering_cases import run_ordering_cases
    from persistence_safety_cases import run_safety_cases
    from persistence_cell_display_cases import run_cell_display_cases
    from persistence_milkdown_input import milkdown_input_semantics
    from persistence_uuid_fixture import build as build_uuid_fixture, inspect as inspect_uuid_fixture
    from persistence_uuid_cases import run_uuid_cases
    from persistence_writer_cases import run_writer_cases
    suite=Suite(Path(args.output), args.scenario)
    suite.fault()
    await suite.renderer_inventory()
    try:
        suite.uuid_fixture = build_uuid_fixture(suite.directory)
        (suite.directory / 'uuid-before.json').write_text(json.dumps(suite.uuid_fixture, indent=2))
        await suite.start()
        suite.uuid_after = inspect_uuid_fixture(suite.directory)
        (suite.directory / 'uuid-after.json').write_text(json.dumps(suite.uuid_after, indent=2))
        suite.uuid_conversion = {'before': str(suite.directory / 'uuid-before.json'),
                                 'after': str(suite.directory / 'uuid-after.json')}
        async with async_playwright() as playwright:
            browser=await playwright.chromium.launch()
            try:
                await run_uuid_cases(suite, browser)
                await run_writer_cases(suite, browser)
                for delay in [.4,1.6,2.6,5.1]:
                    for refused in [False,True]:
                        await suite.check(f'reload-{delay}-{refused}',lambda delay=delay,refused=refused:suite.reload(browser,delay,refused))
                for kind in ['text','milkdown','monaco']:
                    await suite.check(f'large-{kind}',lambda kind=kind:suite.reload(browser,.4,True,large=True,kind=kind))
                    if kind!='text':
                        await suite.check(f'standalone-{kind}',lambda kind=kind:suite.reload(browser,.4,True,kind=kind,fullscreen=True))
                for kind in ['mermaid','slides']:
                    await suite.check(f'editable-{kind}',lambda kind=kind:suite.reload(browser,.4,True,kind=kind))
                boundary_bodies={'empty':'','49-kib':('paragraph text.\n\n'*4000)[:49*1024],
                    '51-kib':('paragraph text.\n\n'*4000)[:51*1024],
                    '210k-characters':('paragraph text.\n\n'*14000)[:210000],
                    'unicode':('日本語🌻 café\n\n'*2500)}
                for kind in ['text','milkdown','monaco']:
                    for label,body in boundary_bodies.items():
                        await suite.check(f'boundary-{kind}-{label}',lambda kind=kind,body=body:suite.reload(browser,.4,True,kind=kind,original=body))
                for fault in [{'delay_before':10},{'refuse':True},{'lose_response':True}]:
                    await suite.check(f'continuous-{next(iter(fault))}',lambda fault=fault:suite.slow_and_concurrent(browser,fault))
                await suite.check('conflict-restart-resolve',lambda:suite.conflict(browser))
                await suite.check('two-windows-conflict-reload',lambda:suite.two_windows(browser))
                await suite.check('indexeddb-abort-recovery',lambda:suite.local_storage_failure(browser))
                await suite.check('two-windows-different-files',lambda:suite.different_windows(browser))
                await suite.check('sse-overflow-reconnect-dirty',lambda:suite.sse_overflow(browser))
                await suite.check('clean-fetch-overtaken-by-typing',lambda:suite.overtaken_read(browser))
                await suite.check('pool-offscreen-warm-park-lru-recovery',lambda:suite.pool_lifetime(browser))
                await suite.check('dirty-rename-save-cancel',lambda:suite.dirty_rename(browser))
                await suite.check('dirty-rename-lost-response-reload',lambda:suite.dirty_rename(browser,True))
                for kind in ['text','milkdown']:
                    await suite.check(f'close-reopen-offline-{kind}',lambda kind=kind:suite.close_reopen(browser,kind))
                await run_renderer_cases(suite,browser)
                await run_ordering_cases(suite,browser)
                await run_safety_cases(suite,browser)
                await run_cell_display_cases(suite,browser)
                await suite.check('milkdown-input-undo-selection-composition',lambda:milkdown_input_semantics(suite,browser))
            finally:await browser.close()
    finally:
        await suite.stop()
        suite.log.close()
    # Coverage is explicit. Additional renderer adapters below must be added
    # when discovery finds a new editable renderer; missing coverage fails.
    covered={row.get('renderer') for row in suite.rows if row['status']=='passed'}
    for item in suite.inventory:
        if item['editable']:
            item['status']='passed' if item['id'] in covered else 'failed'
            item['reason']=None if item['status']=='passed' else 'No successful browser mutation/reload receipt.'
    executed = {row['scenario'] for row in suite.rows}
    missing = [item['scenario'] for item in suite.manifest if item['scenario'] not in executed]
    suite.output.write_text(json.dumps({'scenarios':suite.rows,'scenario_manifest':suite.manifest,
        'unrun_scenarios':missing,'renderer_inventory':suite.inventory,
        'server_log':str(suite.directory/'server.log')},indent=2))
    return int(bool(missing) or any(row['status']=='failed' for row in suite.rows+suite.inventory))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matrix',choices=['full'],default='full')
    parser.add_argument('--output',default='tmp/persistence-1008/torture-results.json')
    parser.add_argument('--scenario',action='append',help='Run matching scenario names for diagnosis; coverage remains incomplete.')
    parser.add_argument('--server',type=int)
    parser.add_argument('--control',type=Path)
    args=parser.parse_args()
    if args.server:server(args.server,args.control)
    else:raise SystemExit(asyncio.run(run(args)))
