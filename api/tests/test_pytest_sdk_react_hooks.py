"""Actual SDK-built hooks are served; deterministic primitives exercise callbacks.

Mounted React/context/race/readiness behavior is covered by the real browser gate.
"""
from sdk_runtime_helpers import run_runtime
from test_pytest_handlers import client as client


def test_all_runtime_hooks_and_primitives_are_callable_R01(client, tmp_path):
    run_runtime(client,tmp_path,'''
for(const name of ['defineRenderer','useCollimateFile','useSiblingFile','useTheme','themeCss','resolveVar','toHex8','useDesktop','useVisibility','useHostFocus','useRemoteModule','useRemoteScript']) assert.equal(typeof sdk[name],'function',name);
assert.throws(()=>sdk.useCollimateFile(),/TOP LEVEL/);
''')


def test_theme_visibility_focus_and_desktop_callbacks_R01(client, tmp_path):
    run_runtime(client,tmp_path,'''
let unsubscribed=0,themeCallback,visibleCallback,focusCallback;
const night={themeId:'night',scheme:'dark',known:true,vars:{'--sheet-2':'#23201c'},fonts:{ui:'U',mono:'M',prose:'P'}};
const paper={themeId:'paper',scheme:'light',known:true,vars:{'--sheet-2':'#f1efea'},fonts:{ui:'U',mono:'M',prose:'P'}};
const api={getTheme:()=>night,onThemeChange:fn=>{themeCallback=fn;return()=>unsubscribed++;},
 getVisible:()=>false,onVisibleChange:fn=>{visibleCallback=fn;return()=>unsubscribed++;},
 getFocused:()=>false,onFocusChange:fn=>{focusCallback=fn;return()=>unsubscribed++;},desktop:{capabilities:{nativeGuests:true}}};
const theme=sdk.useTheme(api);
assert.deepEqual(Object.keys(theme),['themeId','scheme','known','vars','fonts','onChange']);
assert.equal(theme.themeId,'night');assert.equal(theme.scheme,'dark');assert.equal(theme.known,true);
assert.equal(theme.vars['--sheet-2'],'#23201c');assert.deepEqual(theme.fonts,night.fonts);
themeCallback(paper);assert.equal(hooks.states[0],paper);
const heard=[];const off=theme.onChange(t=>heard.push(t));themeCallback(paper);assert.deepEqual(heard,[paper]);off();
const visible=sdk.useVisibility(api);assert.equal(visible.visible,false);visibleCallback(true);assert.equal(hooks.states[1],true);
const focus=sdk.useHostFocus(api);assert.equal(focus.focused,false);focusCallback(true);assert.equal(hooks.states[2],true);
assert.equal(sdk.useDesktop(api),api.desktop);assert.equal(sdk.useDesktop(undefined),undefined);
for(const cleanup of hooks.cleanups)if(cleanup)cleanup();assert.equal(unsubscribed,4);
const placeholder=sdk.useTheme(undefined);assert.equal(placeholder.known,false);assert.equal(placeholder.themeId,'unknown');
assert.equal(sdk.themeCss(paper,{fontsUrl:'https://app.test/fonts/fonts.css'}),
 '@import url("https://app.test/fonts/fonts.css");\\n:root{--sheet-2:#f1efea;color-scheme:light;}');
assert.equal(sdk.toHex8('rgba(198, 157, 100, 0.16)'),'#c69d6429');assert.equal(sdk.resolveVar(paper,'--sheet-2'),'#f1efea');
''')


def test_primary_snapshots_publish_immediately_and_only_host_replacement_changes_content(client,tmp_path):
    run_runtime(client,tmp_path,'''
const saved=[];let file,replacement,readonly=false,unsubscribed=0;
const api={getContent:async()=>'initial',getFilename:()=> 'note.txt',isReadOnly:()=>readonly,
 reportChange:body=>saved.push(body),onSetContent:cb=>{replacement=cb;return()=>unsubscribed++;},ready(){}};
const renderer=sdk.defineRenderer({component:()=>{file=sdk.useCollimateFile();return null;}});
const dispose=await renderer.mount({},api);
assert.equal(file.initialContent,'initial');
file.saveContent('first');file.saveContent('latest');
assert.deepEqual(saved,['first','latest']);assert.equal(hooks.states[0],'latest');
readonly=true;file.saveContent('refused');assert.deepEqual(saved,['first','latest']);
replacement('explicit server replacement');assert.equal(hooks.states[0],'explicit server replacement');
assert.deepEqual(saved,['first','latest']);dispose();assert.equal(unsubscribed,1);
''', render_components=True)


def test_remote_module_reports_failure_and_success_deduplicates(client,tmp_path):
    run_runtime(client,tmp_path,'''
const url='data:text/javascript,globalThis.moduleLoads=(globalThis.moduleLoads||0)+1;export default 42';
sdk.useRemoteModule(url);sdk.useRemoteModule(url);await new Promise(resolve=>setTimeout(resolve,20));
assert.equal(globalThis.moduleLoads,1);assert.equal(hooks.states[0].module.default,42);assert.equal(hooks.states[1].module.default,42);
const bad='data:text/javascript,throw Error("refused")';sdk.useRemoteModule(bad);await new Promise(resolve=>setTimeout(resolve,20));
assert.match(hooks.states[2].error.message,/refused/);
''')


def test_remote_script_failure_removes_node_and_allows_retry(client,tmp_path):
    run_runtime(client,tmp_path,'''
const nodes=[];globalThis.document={querySelector:()=>null,createElement:()=>({remove(){this.removed=true;}}),head:{appendChild:node=>nodes.push(node)}};
sdk.useRemoteScript('https://example.invalid/a.js');assert.equal(nodes.length,1);nodes[0].onerror();
await new Promise(resolve=>setTimeout(resolve,0));assert.equal(nodes[0].removed,true);assert.ok(hooks.states[0].error);
sdk.useRemoteScript('https://example.invalid/a.js');assert.equal(nodes.length,2);nodes[1].onload();
await new Promise(resolve=>setTimeout(resolve,0));assert.equal(hooks.states[1].loading,false);assert.equal(hooks.states[1].error,null);
''')


def test_runtime_routes_serve_exact_sdk_bytes__F0259_F1883(client):
    from collimate.shared import shared_path
    for name in ('react.js', 'session.js'):
        expected = shared_path('renderer-runtime', name).read_bytes()
        response = client.get('/api/sdk/' + name)
        assert response.status_code == 200
        assert response.content == expected
        assert response.headers['content-type'].startswith('application/javascript')


def test_common_preview_graph_serves_exact_inert_sdk_snapshot__P4F04_P5F05(client):
    import hashlib
    import json
    from collimate.shared import shared_path, toolchain
    expected = shared_path('renderer-runtime', 'common.json').read_bytes()
    response = client.get('/api/sdk/preview-common.json')
    assert response.status_code == 200
    assert response.content == expected
    assert response.headers['content-type'].startswith('application/json')
    graph = json.loads(response.content)
    for module in graph.values():
        assert hashlib.sha256(module['body'].encode()).hexdigest() == module['hash']
    react = toolchain()['react']
    assert graph[f'https://esm.sh/react@{react}/es2022/react.mjs'] == graph[f'https://esm.sh/react@{react}?target=es2022']
