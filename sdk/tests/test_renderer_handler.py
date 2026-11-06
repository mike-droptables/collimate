from __future__ import annotations

import json
from pathlib import Path
import time
import pytest
import yaml
from collimate._host.renderer_bundle import compile_verifier
from collimate._host.verification import run_verifier
from collimate.handlers import derived_hash, validate_handler
from tests.conftest import RENDERER_ROOTS
from tests._workspace import BUILTINS

ROOT=Path(__file__).resolve().parents[1]
HANDLER=BUILTINS / 'definitions/renderer'
PRELUDE=ROOT/'shared/verify/prelude.js'

@pytest.fixture(scope='module')
def code():
    return compile_verifier((HANDLER/'derived/verify.ts').read_bytes(), prelude=PRELUDE.read_bytes(), deadline=time.monotonic()+5).code


def invoke(code,text,files,path='pkg/demo.renderer',paths=None):
    return run_verifier(code,{'path':path,'text':text,'package':files,'cell_paths':paths or []},deadline=time.monotonic()+5)


def definition(**changes):
    return yaml.safe_dump(dict(id='demo',name='Demo',summary='A useful renderer',entrypoint='main.tsx',**changes))


@pytest.mark.parametrize('source',[
 'import {defineRenderer} from "/api/sdk/react.js"; export default defineRenderer({component:()=>null})',
 'import {defineRenderer as define} from "/api/sdk/react.js"; const r=define({component:()=>null}); export default r;',
 'export default {__collimate_renderer__:true,component:()=>null};',
])
def test_real_native_renderer_bundle_and_static_shape__D10_D12_F1280_F1984(code,source):
    assert invoke(code,definition(),{'main.tsx':source}) == {'valid':True}


@pytest.mark.parametrize('source,reason',[
 ('export default ()=>null','default export'),
 ('import {defineRenderer} from "/api/sdk/react.js"; export default defineRenderer({})','component'),
 ('import secret from "../secret.ts"; export default secret','Invalid renderer package path'),
 ('export default <',''),
])
def test_real_native_renderer_refusals_never_execute_source__D10_D12_F1280(code,source,reason):
    verdict=invoke(code,definition(),{'main.tsx':source})
    assert verdict['valid'] is False
    assert verdict['error']
    if reason: assert reason in verdict['error']


def test_renderer_sibling_graph_and_no_execution__D10_F1280(code):
    source='import {defineRenderer} from "/api/sdk/react.js"; import {C} from "./component"; throw Error("DO NOT EXECUTE"); export default defineRenderer({component:C});'
    assert invoke(code,definition(),{'main.tsx':source,'component.ts':'export const C=()=>null'}) == {'valid':True}


def test_definition_schema_entrypoint_and_opaque_metadata__D12(code):
    assert invoke(code,'name: Missing fields',{})['valid'] is False
    assert invoke(code,definition(),{'main.tsx':{'size':20,'hash':'a'*64}})['valid'] is False
    assert invoke(code,definition(),{})['valid'] is False
    assert invoke(code,definition(),{'main.tsx':'export default ()=>null','image.png':{'size':10,'hash':'b'*64}})['valid'] is False


def test_preview_schema_and_cell_path_resolution__D12(code):
    text=json.dumps({'renderer':'pkg/demo.renderer','file':'samples/demo.txt'})
    assert invoke(code,text,{},path='demo.renderer_example',paths=['pkg/demo.renderer','samples/demo.txt']) == {'valid':True}
    assert invoke(code,text,{},path='demo.renderer_example',paths=['pkg/demo.renderer'])['valid'] is False
    assert invoke(code,'{"renderer":"../escape.renderer","file":"sample"}',{},path='demo.renderer_example')['valid'] is False


def test_shipped_handler_accepted_hash_and_actual_example__D09_D12(code):
    manifest=yaml.safe_load((HANDLER/'renderer.handler').read_text())
    normalized=validate_handler(manifest,allow_reserved=True)
    files={p.relative_to(HANDLER/'derived').as_posix():p.read_bytes() for p in (HANDLER/'derived').rglob('*') if p.is_file()}
    assert normalized['derived'] == derived_hash(files)
    example=HANDLER/'examples/basic'
    assert invoke(code,(example/'basic.renderer').read_text(),{p.name:p.read_text() for p in example.iterdir()}) == {'valid':True}


def test_all_nineteen_shipped_packages_pass_actual_native_handler__D12_F1280_F1984__D14(code, monkeypatch):
    from collimate.definitions import discover_definitions, package_files
    monkeypatch.setenv('PATH', '')
    checked=[]
    for cell in sorted(cell for root in RENDERER_ROOTS for cell in root.iterdir()):
        if not cell.is_dir(): continue
        paths={p.relative_to(cell).as_posix():p for p in cell.rglob('*') if p.is_file()}
        discovery=discover_definitions(paths)
        assert not discovery.problems
        for package in discovery.packages:
            if package.kind != 'renderer':continue
            files={}
            for path in package_files(package,paths):
                raw=paths[path].read_bytes()
                try: value=raw.decode('utf-8')
                except UnicodeDecodeError: continue
                files[path[len(package.prefix)+1:]]=value
            verdict=invoke(code,paths[package.definition_path].read_text(),files,path=package.definition_path,paths=list(paths))
            assert verdict == {'valid':True}, (package.definition_path,verdict)
            checked.append(package.definition_path)
    assert len(checked)==19


@pytest.mark.parametrize('tail',[
    'const renderer=dr({component:R,diffComponent:R});export default renderer;',
    'const config={component:R};export default dr(config);',
    'const renderer=sdk.defineRenderer({component:R});export {renderer as default};',
    'export default {__collimate_renderer__:true,component:R};',
])
def test_static_shapes_survive_regex_templates_aliases_and_bound_objects__D12_F1280(code,tail):
    source=('import {defineRenderer as dr} from "/api/sdk/react.js"; import * as sdk from "/api/sdk/react.js"; '
            'const RE=/\\/api\\/sdk\\/{1,2}/g; const note="export default nothing"; '
            'function R(p:{n:number}) {const t=`a ${p.n} ${`b${"}"}`}`; return <div title={t}>{String(RE)}{note}</div>;}' + tail)
    assert invoke(code,definition(),{'main.tsx':source}) == {'valid':True}


@pytest.mark.parametrize('specifier',['../host-file.ts','/tmp/host-file.ts','lodash','./_runs/private','bad/api/sdk/react.js'])
def test_actual_handler_import_refusals_are_virtual_only__D12_F1280_P0F21(code,specifier):
    source=f'import {{secret}} from {json.dumps(specifier)}; import {{defineRenderer}} from "/api/sdk/react.js"; export default defineRenderer({{component:()=>secret}});'
    result=invoke(code,definition(),{'main.tsx':source})
    assert result['valid'] is False
    assert result['error']


def test_missing_and_unreadable_default_are_invalid_not_skipped__D12_F1280(code):
    assert invoke(code,definition(),{'main.tsx':'export const named=1'})['valid'] is False
    source='import {defineRenderer} from "/api/sdk/react.js"; const make=c=>defineRenderer({component:c}); export default make(()=>null);'
    result=invoke(code,definition(),{'main.tsx':source})
    assert result['valid'] is False
    assert 'statically readable' in result['error']


def test_native_handler_ignores_host_tsconfig_and_never_executes_marker__D12_F1280(code,tmp_path):
    marker=tmp_path/'executed'
    source=('import {defineRenderer} from "/api/sdk/react.js"; '
            f'process.getBuiltinModule("node:fs").writeFileSync({json.dumps(str(marker))},"must not run"); '
            'export default defineRenderer({component:()=>null});')
    files={'main.tsx':source,'tsconfig.json':'{"extends":"/tmp/unavailable-host-file"}',
           'jsconfig.json':'{"extends":"../unavailable-host-file"}'}
    assert invoke(code,definition(),files) == {'valid':True}
    assert not marker.exists()


def test_definition_package_boundaries_still_select_arbitrary_names__D04(code):
    from collimate.definitions import discover_definitions
    paths={'folder/custom-name.renderer','folder/view.tsx','folder/examples/nested.renderer'}
    found=discover_definitions(paths)
    assert not found.problems
    assert [(p.prefix,p.definition_path) for p in found.packages] == [('folder','folder/custom-name.renderer')]
    files={'view.tsx':'import {defineRenderer} from "/api/sdk/react.js"; export default defineRenderer({component:()=>null});'}
    text=yaml.safe_dump({'id':'declared','name':'Declared','summary':'named definition','entrypoint':'view.tsx'})
    assert invoke(code,text,files,path='folder/custom-name.renderer') == {'valid':True}
    assert discover_definitions(paths|{'folder/other.generator'}).problems[0].code == 'definition-folder-ambiguous'
    assert discover_definitions({'folder/.renderer'}).packages == ()


@pytest.mark.parametrize('entrypoint', ['view:main.tsx', 'nested/view:main.tsx'])
def test_native_and_handler_path_policy_agree_for_literal_colons__D10_D12_F1280(code,entrypoint):
    from collimate._host.renderer_bundle import bundle_renderer
    source='import {defineRenderer} from "/api/sdk/react.js"; import {C} from "./part:component"; export default defineRenderer({component:C});'
    sibling=entrypoint.rsplit('/',1)[0]+'/' if '/' in entrypoint else ''
    files={entrypoint:source,sibling+'part:component.ts':'export const C=()=>null'}
    actual=bundle_renderer({path:text.encode() for path,text in files.items()},entrypoint=entrypoint,deadline=time.monotonic()+5)
    assert actual.entrypoint_js and actual.code
    text=yaml.safe_dump({'id':'demo','name':'Demo','summary':'A useful renderer','entrypoint':entrypoint})
    assert invoke(code,text,files) == {'valid':True}


@pytest.mark.parametrize('entrypoint', ['C:main.tsx','z:/main.tsx','/main.tsx','../main.tsx','a/../main.tsx','a//main.tsx','_runs/main.tsx'])
def test_native_and_handler_keep_unsafe_path_refusals__D10_D12_F1280(code,entrypoint):
    from collimate._host.renderer_bundle import bundle_renderer, RendererCompileError
    files={entrypoint:'export default {__collimate_renderer__:true,component:()=>null}'}
    with pytest.raises(RendererCompileError):
        bundle_renderer({path:text.encode() for path,text in files.items()},entrypoint=entrypoint,deadline=time.monotonic()+5)
    text=yaml.safe_dump({'id':'demo','name':'Demo','summary':'A useful renderer','entrypoint':entrypoint})
    assert invoke(code,text,files)['valid'] is False
