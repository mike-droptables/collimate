"""The SDK owns built renderer runtime bytes consumed by the app."""
from pathlib import Path
import pytest
from collimate.shared import shared_path, toolchain


def runtime_react_urls() -> tuple[str, str]:
    """The runtime's React and ReactDOM client URLs, from the toolchain contract."""
    pins = toolchain()
    return (f"https://esm.sh/react@{pins['react']}?target=es2022",
            f"https://esm.sh/react-dom@{pins['react_dom']}/client?deps=react@{pins['react']}&target=es2022")


def test_canonical_served_runtime_assets_exist__F0259_F1807_F1828_F1847_F1883():
    for name in ('react.js', 'session.js', 'common.json', 'snapshot-codecs.json'):
        body = shared_path('renderer-runtime', name).read_bytes()
        assert body
    assert b'import ' not in shared_path('renderer-runtime', 'session.js').read_bytes()


def test_built_runtime_bytes_match_canonical_sources__F1883():
    import subprocess
    root = Path(__file__).resolve().parents[1]
    checked = subprocess.run(['node', str(root/'scripts/build_renderer_runtime.mjs'), '--check'], capture_output=True, text=True, timeout=20)
    assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize('source_name', ['platform.tsx', 'media.tsx'])
def test_runtime_build_rejects_source_or_asset_drift__F1883(tmp_path, source_name):
    import shutil
    from collimate.renderer_runtime import verify_runtime_resources
    root = Path(__file__).resolve().parents[1]
    for name in ('shared/renderer-runtime', 'packages/renderer-hooks/src'):
        shutil.copytree(root / name, tmp_path / name)
    for name in ('scripts/build_renderer_runtime.mjs', 'src/collimate/renderer_bundle_contract.json',
                 'shared/contracts/toolchain.json', 'scripts/build_snapshot_codecs.mjs', 'scripts/snapshot-codecs/milkdown.ts',
                 'scripts/package.json', 'scripts/package-lock.json',
                 'scripts/snapshot-codecs/embed-markdown.ts',
                 'scripts/snapshot-codecs/snapshot-model.ts'):
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, dest)
    assert set(verify_runtime_resources(tmp_path)) == {'react.js', 'session.js', 'common.json', 'snapshot-codecs.json'}
    source = tmp_path / 'packages/renderer-hooks/src' / source_name
    original = source.read_bytes()
    source.write_bytes(original + b'\n// changed authored source\n')
    with pytest.raises(ValueError, match='source changed'):
        verify_runtime_resources(tmp_path)
    source.write_bytes(original)
    (tmp_path / 'shared/renderer-runtime/react.js').write_bytes(b'wrong compiled bytes')
    with pytest.raises(ValueError, match='resource mismatch'):
        verify_runtime_resources(tmp_path)


def test_installed_runtime_verifies_without_node__F1883(monkeypatch):
    """The bytes the host serves verify against the runtime manifest with no
    Node on PATH: an installed SDK needs no compiler to serve them."""
    from collimate.renderer_runtime import verify_runtime_resources
    monkeypatch.setenv('PATH', '')
    bodies = verify_runtime_resources()
    assert set(bodies) == {'react.js', 'session.js', 'common.json', 'snapshot-codecs.json'}
    for name, body in bodies.items():
        assert body and body == shared_path('renderer-runtime', name).read_bytes()


def _build_outputs():
    import importlib.util
    import sys
    path = Path(__file__).resolve().parents[1] / 'scripts/build_outputs.py'
    spec = importlib.util.spec_from_file_location('build_outputs', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_built_outputs_are_git_ignored_and_pinned_by_committed_manifests():
    """The runtime and the prelude are build outputs: git-ignored (sdk/.gitignore
    names exactly build_outputs.BUILT), while the manifests that pin them, and
    every output a manifest names that is not built there, are committed."""
    import json
    import subprocess
    root = Path(__file__).resolve().parents[1]
    built = _build_outputs().BUILT
    ignored = [line for line in (root / '.gitignore').read_text().splitlines() if line.startswith('/shared/')]
    assert sorted(ignored) == sorted('/' + name for name in built)
    def tracked(name):
        return subprocess.run(['git', '-C', str(root), 'ls-files', '--error-unmatch', name],
                              capture_output=True).returncode == 0
    for name in built:
        assert subprocess.run(['git', '-C', str(root), 'check-ignore', '-q', name]).returncode == 0, name
        assert not tracked(name), name
    for manifest in ('shared/renderer-runtime/index.json', 'shared/verify/prelude.json'):
        assert tracked(manifest), manifest
        folder = Path(manifest).parent.as_posix()
        for output in json.loads((root / manifest).read_bytes())['outputs']:
            assert (f'{folder}/{output}' in built) != tracked(f'{folder}/{output}'), output


def test_the_outputs_build_rebuilds_to_the_manifests_and_never_writes_one(tmp_path, monkeypatch):
    """build_outputs.py (prepare_sdk_checkout.sh, hatch_build.py) rebuilds a
    missing output to its committed manifest's bytes with ``--outputs``, needs
    no Node once they match, and refuses a source edited since its manifest
    without touching the manifest."""
    import shutil
    root = Path(__file__).resolve().parents[1]
    sdk = tmp_path / 'sdk'
    for directory in ('shared/renderer-runtime', 'shared/verify', 'shared/contracts', 'packages/renderer-hooks/src',
                      'scripts/snapshot-codecs'):
        shutil.copytree(root / directory, sdk / directory)
    for filename in ('scripts/build_renderer_runtime.mjs', 'scripts/build_snapshot_codecs.mjs',
                     'scripts/build_verify_prelude.mjs', 'scripts/package.json', 'scripts/package-lock.json',
                     'src/collimate/renderer_bundle_contract.json'):
        (sdk / filename).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / filename, sdk / filename)
    for modules in ('scripts/node_modules', 'packages/renderer-hooks/node_modules'):
        (sdk / modules).symlink_to(root / modules, target_is_directory=True)
    build = _build_outputs()
    manifests = {name: (sdk / name).read_bytes() for name in ('shared/renderer-runtime/index.json',
                                                              'shared/verify/prelude.json',
                                                              'shared/verify/dependency-licenses.txt')}
    for name in build.BUILT:
        (sdk / name).unlink()
    assert build.ensure(sdk) == ['renderer runtime', 'verifier prelude']
    for name in build.BUILT:
        assert (sdk / name).read_bytes() == (root / name).read_bytes(), name
    assert {name: (sdk / name).read_bytes() for name in manifests} == manifests
    assert not (tmp_path / 'content').exists()  # --outputs writes no codec descriptor either
    monkeypatch.setenv('PATH', '')
    assert build.ensure(sdk) == []  # current: no Node needed
    monkeypatch.undo()
    source = sdk / 'packages/renderer-hooks/src/media.tsx'
    source.write_bytes(source.read_bytes() + b'\n// edited since the manifest\n')
    (sdk / 'shared/renderer-runtime/react.js').unlink()
    with pytest.raises(build.BuildRefused, match='generate.py --only renderer-runtime'):
        build.ensure(sdk)
    assert {name: (sdk / name).read_bytes() for name in manifests} == manifests
    assert not (sdk / 'shared/renderer-runtime/react.js').exists()


def test_milkdown_renderer_carries_the_runtime_codec_sources_byte_for_byte():
    """The snapshot codec's canonical sources are the runtime's
    (scripts/snapshot-codecs/); the milkdown renderer, whose editor must
    serialize exactly what the codec does, keeps byte-identical copies."""
    from tests._workspace import RENDERERS
    root = Path(__file__).resolve().parents[1]
    for name in ('embed-markdown.ts', 'snapshot-model.ts'):
        canonical = (root / 'scripts/snapshot-codecs' / name).read_bytes()
        assert (RENDERERS / 'documents/milkdown' / name).read_bytes() == canonical, (
            f'content/renderers/documents/milkdown/{name} differs from sdk/scripts/snapshot-codecs/{name}: '
            'edit the SDK copy and copy it to the renderer')


def test_shipped_renderer_and_runtime_graphs_share_exact_react_identity__F1869():
    import json
    import re
    root = Path(__file__).resolve().parents[1]
    pins = toolchain()
    assert pins['react'] == pins['react_dom'] and pins['types_react'] == pins['types_react_dom']
    react, dom = runtime_react_urls()
    runtime = shared_path('renderer-runtime', 'react.js').read_text()
    assert react in runtime and dom in runtime
    from tests.conftest import RENDERER_ROOTS
    definitions = sorted(d for tree in RENDERER_ROOTS for d in tree.glob('*/*/*.renderer'))
    assert len(definitions) == 19
    for definition in definitions:
        source = (definition.parent / 'main.tsx').read_text()
        assert react in source, definition
        urls = re.findall(r'https://esm\.sh/[^\s\'";]+', source)
        for url in urls:
            assert not re.search(r'react(?:-dom)?@18(?:[/?&,]|$)', url), (definition, url)
            if 'deps=' in url and 'react@' in url:
                assert f"react@{pins['react']}" in url, (definition, url)
            if 'react-dom@' in url:
                assert f"react-dom@{pins['react_dom']}" in url and f"react@{pins['react']}" in url, (definition, url)
    package = json.loads((root / 'packages/renderer-hooks/package.json').read_bytes())
    assert (package['devDependencies']['react'], package['devDependencies']['react-dom']) == (pins['react'], pins['react_dom'])
    assert (package['devDependencies']['@types/react'], package['devDependencies']['@types/react-dom']) == (
        pins['types_react'], pins['types_react_dom'])
    assert package['peerDependencies'] == {'react': '>=19', 'react-dom': '>=19'}


def test_fresh_common_build_requires_hooks_install_before_runtime__P5F05(tmp_path):
    import shutil
    import subprocess
    from tests._workspace import RENDERERS
    root = Path(__file__).resolve().parents[1]
    # A scratch workspace: the SDK member's runtime sources, and the milkdown
    # renderer's built codec the runtime build checks beside it.
    sdk = tmp_path / 'sdk'
    for directory in ('shared/renderer-runtime', 'packages/renderer-hooks/src'):
        shutil.copytree(root / directory, sdk / directory)
    for filename in ('scripts/build_renderer_runtime.mjs', 'scripts/build_snapshot_codecs.mjs',
                     'scripts/snapshot-codecs/milkdown.ts', 'scripts/package.json', 'scripts/package-lock.json',
                     'scripts/snapshot-codecs/embed-markdown.ts',
                     'scripts/snapshot-codecs/snapshot-model.ts',
                     'src/collimate/renderer_bundle_contract.json', 'shared/contracts/toolchain.json'):
        target = sdk / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / filename, target)
    descriptor = tmp_path / 'content/renderers/documents/milkdown/snapshot-codec.ts'
    descriptor.parent.mkdir(parents=True)
    shutil.copyfile(RENDERERS / 'documents/milkdown/snapshot-codec.ts', descriptor)
    (sdk / 'scripts/node_modules').symlink_to(root / 'scripts/node_modules', target_is_directory=True)
    command = ['node', str(sdk / 'scripts/build_renderer_runtime.mjs'), '--check']
    red = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert red.returncode != 0 and 'react.production.js' in red.stderr
    (sdk / 'packages/renderer-hooks/node_modules').symlink_to(root / 'packages/renderer-hooks/node_modules', target_is_directory=True)
    green = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert green.returncode == 0, green.stdout + green.stderr
    # The installed React is the contract's, or the build refuses.
    contract = sdk / 'shared/contracts/toolchain.json'
    contract.write_text(contract.read_text().replace(f'"react": "{toolchain()["react"]}"', '"react": "0.0.0-test"'))
    drifted = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert drifted.returncode != 0 and 'the toolchain contract says 0.0.0-test' in drifted.stderr


def _run_built_runtime(tmp_path, assertions):
    """Import the served react.js with deterministic React primitives."""
    import json
    import subprocess
    react = tmp_path / 'react.mjs'
    react.write_text('''export const effects=[],cleanups=[],states=[],refs=[];
export function createContext(value){return {value};}
export function useContext(context){return context.value;}
export function useState(initial){const index=states.length;states.push(typeof initial==='function'?initial():initial);return [states[index],value=>{states[index]=value;}];}
export function useEffect(fn){effects.push(fn);cleanups.push(fn());}
export function useLayoutEffect(fn){effects.push(fn);cleanups.push(fn());}
export function useCallback(fn){return fn;}
export function useMemo(fn){return fn();}
export function useRef(value){const ref={current:value};refs.push(ref);return ref;}
export default {Component:class {},createElement(){}};
''')
    dom = tmp_path / 'dom.mjs'
    dom.write_text('export function createRoot(){throw Error("no DOM here")}')
    body = shared_path('renderer-runtime', 'react.js').read_text()
    react_url, dom_url = runtime_react_urls()
    body = body.replace(dom_url, dom.as_uri())
    body = body.replace(react_url, react.as_uri())
    module = tmp_path / 'runtime.mjs'
    module.write_text(body)
    script = tmp_path / 'run.mjs'
    script.write_text("import assert from 'node:assert/strict';\nimport * as hooks from " + json.dumps(react.as_uri())
                      + ";\nimport * as sdk from " + json.dumps(module.as_uri()) + ";\n" + assertions)
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


# The record the app sends (W1): every skin token by CSS name.
_THEME_VAR_NAMES = ['--canvas', '--sheet', '--sheet-2', '--sheet-3', '--line', '--line-2',
                    '--ink', '--ink-2', '--ink-3', '--brass', '--brass-soft',
                    '--teal', '--teal-soft', '--teal-sheet', '--red', '--red-soft', '--green',
                    '--code-bg', '--prose-ink', '--shadow', '--lift',
                    '--font-ui', '--font-mono', '--font-prose', '--radius']


def test_theme_var_name_union_matches_the_runtime_list_R01():
    import re
    root = Path(__file__).resolve().parents[1]
    types = (root / 'packages/renderer-hooks/src/types.ts').read_text()
    union = types.split('export type ThemeVarName =', 1)[1].split(';', 1)[0]
    assert re.findall(r"'(--[a-z0-9-]+)'", union) == _THEME_VAR_NAMES
    index = (root / 'packages/renderer-hooks/src/index.tsx').read_text()
    listed = index.split('export const THEME_VAR_NAMES', 1)[1].split('] as const', 1)[0]
    assert re.findall(r"'(--[a-z0-9-]+)'", listed) == _THEME_VAR_NAMES


def test_built_runtime_tokens_are_the_skin_vocabulary_R01(tmp_path):
    import json
    _run_built_runtime(tmp_path, '''
const names = ''' + json.dumps(_THEME_VAR_NAMES) + ''';
assert.deepEqual([...sdk.THEME_VAR_NAMES], names);
assert.ok(Object.isFrozen(sdk.tokens) && Object.isFrozen(sdk.tokens.color) && Object.isFrozen(sdk.tokens.font));
assert.deepEqual(Object.keys(sdk.tokens), ['color','font','radius','shadow','lift']);
assert.deepEqual(Object.keys(sdk.tokens.color), ['canvas','sheet','sheet2','sheet3','line','line2','ink','ink2','ink3',
  'brass','brassSoft','teal','tealSoft','tealSheet','red','redSoft','green','codeBg','proseInk']);
assert.deepEqual(sdk.tokens.font, {ui:'var(--font-ui)', mono:'var(--font-mono)', prose:'var(--font-prose)'});
assert.equal(sdk.tokens.color.sheet2, 'var(--sheet-2)');
assert.equal(sdk.tokens.color.codeBg, 'var(--code-bg)');
assert.equal(sdk.tokens.radius, 'var(--radius)');
assert.equal(sdk.tokens.shadow, 'var(--shadow)');
assert.equal(sdk.tokens.lift, 'var(--lift)');
// Every reference names a declared token.
const refs = [...Object.values(sdk.tokens.color), ...Object.values(sdk.tokens.font), sdk.tokens.radius, sdk.tokens.shadow, sdk.tokens.lift];
for (const ref of refs) assert.ok(names.includes(ref.slice(4, -1)), ref);
assert.throws(() => { sdk.tokens.color.ink = '#000'; }, TypeError);
''')


def test_built_runtime_use_theme_carries_the_whole_record_R01(tmp_path):
    _run_built_runtime(tmp_path, '''
const night = {themeId:'night', scheme:'dark', known:true, vars:{'--sheet-2':'#23201c'}, fonts:{ui:'U', mono:'M', prose:'P'}};
const paper = {themeId:'paper', scheme:'light', known:true, vars:{'--sheet-2':'#f1efea'}, fonts:{ui:'U', mono:'M', prose:'P'}};
let pushed, unsubscribed = 0, heard = [];
const api = {getTheme: () => night, onThemeChange: fn => { pushed = fn; return () => unsubscribed++; }};
const theme = sdk.useTheme(api);
assert.equal(theme.themeId, 'night'); assert.equal(theme.scheme, 'dark'); assert.equal(theme.known, true);
assert.equal(theme.vars['--sheet-2'], '#23201c'); assert.deepEqual(theme.fonts, night.fonts);
pushed(paper);
assert.equal(hooks.states[0], paper);
const off = theme.onChange(t => heard.push(t)); pushed(paper);
assert.deepEqual(heard, [paper]); off();
for (const cleanup of hooks.cleanups) if (cleanup) cleanup();
assert.equal(unsubscribed, 2);
// No host theme: the placeholder, and no subscription.
const none = sdk.useTheme(undefined);
assert.equal(none.known, false); assert.equal(none.themeId, 'unknown'); assert.equal(none.scheme, 'dark');
assert.deepEqual(none.vars, {}); assert.deepEqual(none.fonts, {ui:'', mono:'', prose:''});
assert.equal(none.onChange(() => {}), undefined);
''')


def test_built_runtime_use_editing_follows_the_host_preferences(tmp_path):
    _run_built_runtime(tmp_path, '''
let pushed, unsubscribed = 0, heard = [];
const api = {getEditing: () => ({spellcheck: false}), onEditingChange: fn => { pushed = fn; return () => unsubscribed++; }};
const editing = sdk.useEditing(api);
assert.equal(editing.spellcheck, false);
pushed({spellcheck: true});
assert.deepEqual(hooks.states[0], {spellcheck: true});
const off = editing.onChange(e => heard.push(e)); pushed({spellcheck: false});
assert.deepEqual(heard, [{spellcheck: false}]); off();
for (const cleanup of hooks.cleanups) if (cleanup) cleanup();
assert.equal(unsubscribed, 2);
// No host preferences: the browser default, and no subscription.
const none = sdk.useEditing(undefined);
assert.equal(none.spellcheck, true);
assert.equal(none.onChange(() => {}), undefined);
''')


def test_built_runtime_use_style_follows_the_host_RS01(tmp_path):
    _run_built_runtime(tmp_path, """
let pushed, unsubscribed = 0, heard = [], patches = [];
const api = {getStyle: () => ({theme: 'nord', font: null, textSize: 1.3}),
             onStyleChange: fn => { pushed = fn; return () => unsubscribed++; },
             setStyle: patch => patches.push(patch)};
const style = sdk.useStyle(api);
assert.equal(style.theme, 'nord'); assert.equal(style.font, null); assert.equal(style.textSize, 1.3);
pushed({theme: null, font: 'Lora', textSize: null});
assert.deepEqual(hooks.states[0], {theme: null, font: 'Lora', textSize: null});
// A record the host could not have sent reads as the defaults, field by field.
pushed({theme: '', font: 7, textSize: Infinity});
assert.deepEqual(hooks.states[0], {theme: null, font: null, textSize: null});
style.set({textSize: 1.2}); style.set({theme: null});
assert.deepEqual(patches, [{textSize: 1.2}, {theme: null}]);
const off = style.onChange(s => heard.push(s)); pushed({theme: 'frame', font: null, textSize: 0.9});
assert.deepEqual(heard, [{theme: 'frame', font: null, textSize: 0.9}]); off();
for (const cleanup of hooks.cleanups) if (cleanup) cleanup();
assert.equal(unsubscribed, 2);
// No style members: every field the renderer's default, set and onChange inert.
const none = sdk.useStyle(undefined);
assert.deepEqual([none.theme, none.font, none.textSize], [null, null, null]);
none.set({theme: 'nord'});
assert.equal(none.onChange(() => {}), undefined);
assert.equal(typeof sdk.StyleControl, 'function');
""")


def test_built_runtime_cell_file_drop_accepts_only_this_cells_files_RS01(tmp_path):
    _run_built_runtime(tmp_path, """
assert.equal(sdk.CELL_FILES_MIME, 'application/x-collimate-files');
const drag = (types, data) => ({types, getData: t => (t in data ? data[t] : '')});
const api = {getCellId: () => 'cell-a'};
const mine = drag(['text/plain', sdk.CELL_FILES_MIME],
                  {[sdk.CELL_FILES_MIME]: JSON.stringify({cellId: 'cell-a', paths: ['notes.md', 'img/plot.png']})});
assert.equal(sdk.isCellFileDrag(mine), true);
assert.deepEqual(sdk.readCellFileDrop(mine, api), ['notes.md', 'img/plot.png']);
// dragover sees the types only: a drag whose data cannot be read yet still counts.
assert.equal(sdk.isCellFileDrag(drag([sdk.CELL_FILES_MIME], {})), true);
assert.equal(sdk.isCellFileDrag(drag(['Files'], {})), false);
assert.equal(sdk.isCellFileDrag(null), false);
// Another cell's files, a malformed payload, a foreign drag, no cell: refused.
const other = drag([sdk.CELL_FILES_MIME], {[sdk.CELL_FILES_MIME]: JSON.stringify({cellId: 'cell-b', paths: ['x.md']})});
assert.equal(sdk.readCellFileDrop(other, api), null);
assert.equal(sdk.readCellFileDrop(drag([sdk.CELL_FILES_MIME], {[sdk.CELL_FILES_MIME]: '1'}), api), null);
assert.equal(sdk.readCellFileDrop(drag([sdk.CELL_FILES_MIME], {[sdk.CELL_FILES_MIME]: '{'}), api), null);
assert.equal(sdk.readCellFileDrop(drag([sdk.CELL_FILES_MIME],
  {[sdk.CELL_FILES_MIME]: JSON.stringify({cellId: 'cell-a', paths: 'x.md'})}), api), null);
assert.equal(sdk.readCellFileDrop(drag(['text/plain'], {'text/plain': 'x'}), api), null);
assert.equal(sdk.readCellFileDrop(mine, {}), null);
assert.equal(sdk.readCellFileDrop(mine, undefined), null);
// Only non-empty string paths survive; none left is no drop.
const mixed = drag([sdk.CELL_FILES_MIME], {[sdk.CELL_FILES_MIME]: JSON.stringify({cellId: 'cell-a', paths: ['a.md', '', 3, null]})});
assert.deepEqual(sdk.readCellFileDrop(mixed, api), ['a.md']);
const empty = drag([sdk.CELL_FILES_MIME], {[sdk.CELL_FILES_MIME]: JSON.stringify({cellId: 'cell-a', paths: []})});
assert.equal(sdk.readCellFileDrop(empty, api), null);
""")


def test_style_and_drop_members_are_served_and_declared_RS01():
    """The style hook, the control and the drop helpers are in the served
    runtime's export list and declared for renderer typechecks; the stored
    dark override and its pill are gone from both."""
    import re
    root = Path(__file__).resolve().parents[1]
    runtime = (root / 'packages/renderer-hooks/src/runtime.tsx').read_text()
    declared = set(re.findall(r'^export (?:function|const) (\w+)',
                              (root / 'packages/renderer-hooks/platform.d.ts').read_text(), re.M))
    built = shared_path('renderer-runtime', 'react.js').read_text()
    served = set(re.findall(r'\b(\w+)\b', built[built.rindex('export {'):]))
    for name in ('useStyle', 'StyleControl', 'StyleControlRow', 'CELL_FILES_MIME', 'isCellFileDrag', 'readCellFileDrop'):
        assert re.search(r'\b' + name + r'\b', runtime), name
        assert name in declared, name
        assert name in served, name
    gone = ('Document' + 'DarkPill', 'use' + 'DarkOverride', 'parse' + 'DarkOverride', 'DARK_' + 'OVERRIDE_KEY')
    for name in gone:
        assert name not in runtime and name not in declared and name not in served, name
    types = (root / 'packages/renderer-hooks/src/types.ts').read_text()
    api = types.split('export interface RendererAPI {', 1)[1].split('\n}', 1)[0]
    for member in ('getStyle(): RendererStyle;', 'onStyleChange(cb: (s: RendererStyle) => void): () => void;',
                   'setStyle(patch: Partial<RendererStyle>): void;',
                   'getFontFamilies(): ReadonlyArray<RendererFontFamily>;'):
        assert member in api, member


def test_annotation_members_are_served_and_declared():
    """The annotation hook and its rejection are in the served runtime's
    export list and declared for renderer typechecks; the RendererAPI member
    and the two request types are the contract the hosts implement. The
    retired document-view members are gone from all of them."""
    import re
    root = Path(__file__).resolve().parents[1]
    runtime = (root / 'packages/renderer-hooks/src/runtime.tsx').read_text()
    platform = (root / 'packages/renderer-hooks/platform.d.ts').read_text()
    declared = set(re.findall(r'^export (?:function|const) (\w+)', platform, re.M))
    built = shared_path('renderer-runtime', 'react.js').read_text()
    served = set(re.findall(r'\b(\w+)\b', built[built.rindex('export {'):]))
    for name in ('useAnnotationRequest', 'annotationUnavailable'):
        assert re.search(r'\b' + name + r'\b', runtime), name
        assert name in declared, name
        assert name in served, name
    for name in ('STATUS' + '_THEME_VARS', 'statusVariable' + 'Decls', 'STATUS' + '_DARKREADER_FIXES', 'DarkReader' + 'Fixes'):
        assert name not in runtime and name not in platform and name not in built, name
    types = (root / 'packages/renderer-hooks/src/types.ts').read_text()
    api = types.split('export interface RendererAPI {', 1)[1].split('\n}', 1)[0]
    assert ('    onAnnotationRequest?(handler: (request: AnnotationRequest, signal: AbortSignal) => '
            'Promise<AnnotationContext | null>): () => void;') in api

    def fields(name):
        body = types.split(f'export interface {name} {{', 1)[1].split('\n}', 1)[0]
        return re.findall(r'^    (\w+)\??:', body, re.M)
    assert fields('AnnotationRequest') == ['id', 'mode', 'hint']
    assert fields('AnnotationContext') == ['label', 'text', 'anchor']
    surface = types.split('export interface SurfaceSession {', 1)[1].split('\n}', 1)[0]
    assert re.findall(r'^    (\w+):', surface, re.M) == ['state', 'chat_id', 'ws_path', 'token', 'label', 'run_id']


def test_built_runtime_use_annotation_request_follows_the_latest_handler(tmp_path):
    _run_built_runtime(tmp_path, """
let subscribed = [], unsubscribed = 0;
const api = {onAnnotationRequest: handler => { subscribed.push(handler); return () => unsubscribed++; }};
const first = async request => ({label: 'first ' + request.id, text: 'a'});
const later = async request => ({label: 'later ' + request.id, text: 'b'});
sdk.useAnnotationRequest(api, first);
assert.equal(subscribed.length, 1);
const signal = new AbortController().signal;
assert.deepEqual(await subscribed[0]({id: 'r1', mode: 'pick'}, signal), {label: 'first r1', text: 'a'});
// The subscription calls whatever handler the view rendered last.
hooks.refs.at(-1).current = later;
assert.deepEqual(await subscribed[0]({id: 'r2', mode: 'selection'}, signal), {label: 'later r2', text: 'b'});
hooks.refs.at(-1).current = null;
assert.equal(await subscribed[0]({id: 'r3', mode: 'pick'}, signal), null);
for (const cleanup of hooks.cleanups) if (cleanup) cleanup();
assert.equal(unsubscribed, 1);
// A null handler takes no requests; a host without the member is inert.
sdk.useAnnotationRequest(api, null);
assert.equal(subscribed.length, 1);
sdk.useAnnotationRequest({}, first);
sdk.useAnnotationRequest(undefined, first);
assert.equal(subscribed.length, 1);
const error = sdk.annotationUnavailable('x');
assert.ok(error instanceof Error);
assert.equal(error.name, 'AnnotationUnavailable');
assert.equal(error.message, 'x');
""")


def test_style_control_paints_only_from_skin_tokens_RS01():
    """The control runs in a shadow root and in an iframe: inline styles,
    every colour a skin token, its own icon, and the outside press and Esc
    heard on its root node."""
    import re
    root = Path(__file__).resolve().parents[1]
    source = (root / 'packages/renderer-hooks/src/style-control.tsx').read_text()
    assert 'className' not in source
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|color-mix', source)
    for colour in re.findall(r"(?:color|background|border)\w*: '([^']*)'", source):
        assert colour in ('none', 'currentColor') or 'var(--' in colour, colour
    assert 'el.getRootNode()' in source and "'pointerdown'" in source and "'Escape'" in source
    assert 'useStyle(api)' in source and 'Skin default' in source


def test_built_runtime_theme_css_and_colour_helpers_R01(tmp_path):
    _run_built_runtime(tmp_path, '''
const theme = {themeId:'paper', scheme:'light', known:true,
  vars:{'--sheet':'#f8f7f3', '--canvas':'#e6e4de', '--font-prose':'"Alegreya", Georgia, serif',
        '--red':'red;}body{color:red', '--not-a-token':'#000', '--lift':'rgba(40, 36, 28, 0.22)'},
  fonts:{ui:'', mono:'', prose:'"Alegreya", Georgia, serif'}};
const css = sdk.themeCss(theme, {fontsUrl:'https://app.test/fonts/fonts.css'});
assert.equal(css, '@import url("https://app.test/fonts/fonts.css");\\n' +
  ':root{--canvas:#e6e4de;--sheet:#f8f7f3;--lift:rgba(40, 36, 28, 0.22);--font-prose:"Alegreya", Georgia, serif;color-scheme:light;}');
assert.equal(sdk.themeCss({...theme, scheme:'dark', vars:{}}), ':root{color-scheme:dark;}');
assert.equal(sdk.resolveVar(theme, '--sheet'), '#f8f7f3');
assert.equal(sdk.resolveVar(theme, '--ink'), '');
assert.equal(sdk.toHex8('#1c1a17'), '#1c1a17ff');
assert.equal(sdk.toHex8('#ABC'), '#aabbccff');
assert.equal(sdk.toHex8('#abcd'), '#aabbccdd');
assert.equal(sdk.toHex8('#11223344'), '#11223344');
assert.equal(sdk.toHex8('rgba(198, 157, 100, 0.16)'), '#c69d6429');
assert.equal(sdk.toHex8('rgb(0 0 0 / 50%)'), '#00000080');
assert.equal(sdk.toHex8('rgb(255, 0, 0)'), '#ff0000ff');
assert.equal(sdk.toHex8('color-mix(in srgb, red, blue)'), null);
assert.equal(sdk.toHex8('#12345'), null);
''')


def test_theme_contract_files_name_no_retired_token_R01():
    import re
    root = Path(__file__).resolve().parents[1]
    retired = re.compile(r'void-?main|voidMain|obsidian|glass-?panel|filament|electric-?teal|electricTeal|plasma|'
                         r'tungsten|signal-?white|signalWhite|comment-?grey|commentGrey|renderer-surface|'
                         r'muted-foreground|mutedForeground|--destructive|--font-sans|--radius-(sm|lg)|050505|camelCase')
    for rel in ('packages/renderer-hooks/src/types.ts', 'packages/renderer-hooks/src/index.tsx',
                'packages/renderer-hooks/src/runtime.tsx', 'packages/renderer-hooks/src/style-control.tsx',
                'packages/renderer-hooks/platform.d.ts',
                'packages/renderer-hooks/README.md', 'scripts/bundle_renderer_test.mjs',
                'shared/docs/how-to-write-a-renderer.md', 'shared/docs/authoring-best-practices.md'):
        text = (root / rel).read_text()
        assert not retired.search(text), (rel, retired.search(text).group(0))


# The three built-in skins' colour tokens, as the host sends them (W1).
_SKIN_FIXTURES = {
    'night': ('dark', {
        '--canvas': '#151311', '--sheet': '#1c1a17', '--sheet-2': '#23201c', '--sheet-3': '#2a2621',
        '--line': '#312d27', '--line-2': '#3f3931', '--ink': '#e2ddd4', '--ink-2': '#a39d92',
        '--ink-3': '#6f695f', '--brass': '#c69d64', '--brass-soft': 'rgba(198, 157, 100, 0.16)',
        '--teal': '#7fb0ab', '--teal-soft': 'rgba(127, 176, 171, 0.14)', '--teal-sheet': '#1a211f',
        '--red': '#cf8b7c', '--red-soft': 'rgba(207, 139, 124, 0.16)', '--green': '#90b893',
        '--code-bg': '#121008', '--prose-ink': '#e9e4da'}),
    'night-cool': ('dark', {
        '--canvas': '#10161b', '--sheet': '#171d23', '--sheet-2': '#1d252c', '--sheet-3': '#232c34',
        '--line': '#2a333b', '--line-2': '#344049', '--ink': '#dbdfe3', '--ink-2': '#99a3ab',
        '--ink-3': '#626e77', '--brass': '#b9966b', '--brass-soft': 'rgba(185, 150, 107, 0.14)',
        '--teal': '#76a9a6', '--teal-soft': 'rgba(118, 169, 166, 0.14)', '--teal-sheet': '#172226',
        '--red': '#c9857a', '--red-soft': 'rgba(201, 133, 122, 0.16)', '--green': '#88b592',
        '--code-bg': '#131920'}),
    'paper': ('light', {
        '--canvas': '#e6e4de', '--sheet': '#f8f7f3', '--sheet-2': '#f1efea', '--sheet-3': '#e9e7e1',
        '--line': '#d4d0c7', '--line-2': '#c4bfb5', '--ink': '#1e2227', '--ink-2': '#5d646b',
        '--ink-3': '#9a9fa4', '--brass': '#8a6d3b', '--brass-soft': 'rgba(138, 109, 59, 0.12)',
        '--teal': '#2e7773', '--teal-soft': 'rgba(46, 119, 115, 0.10)', '--teal-sheet': '#f1f5f3',
        '--red': '#a3463a', '--red-soft': 'rgba(163, 70, 58, 0.10)', '--green': '#3d7750',
        '--code-bg': '#efede7'}),
}


def test_built_runtime_syntax_palette_derives_every_role_from_the_skin_R05_W2b(tmp_path):
    import json
    skins = {name: {'themeId': name, 'scheme': scheme, 'known': True, 'vars': varz,
                    'fonts': {'ui': '', 'mono': '', 'prose': ''}}
             for name, (scheme, varz) in _SKIN_FIXTURES.items()}
    _run_built_runtime(tmp_path, 'const skins = ' + json.dumps(skins) + ''';
const roles = ['comment','keyword','string','number','type','function','variable','operator',
  'punctuation','tag','attribute','regexp','invalid','added','removed','modified'];
assert.deepEqual([...sdk.SYNTAX_ROLES], roles);
assert.ok(Object.isFrozen(sdk.SYNTAX_ROLES));
// WCAG 2 contrast, computed here independently of the runtime.
const lin = c => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const lum = h => [1, 3, 5].map(i => lin(parseInt(h.slice(i, i + 2), 16) / 255))
  .reduce((s, c, i) => s + c * [0.2126, 0.7152, 0.0722][i], 0);
const ratio = (x, y) => { const [a, b] = [lum(x), lum(y)].sort((p, q) => q - p); return (a + 0.05) / (b + 0.05); };
const L = h => lum(h);
for (const [id, skin] of Object.entries(skins)) {
  const p = sdk.syntaxPalette(skin);
  assert.ok(p && Object.isFrozen(p), id);
  assert.deepEqual(Object.keys(p), roles, id);
  for (const role of roles) {
    assert.match(p[role], /^#[0-9a-f]{6}$/, `${id} ${role}`);
    // The floor holds on both code grounds: the editor's and code blocks'.
    for (const ground of ['--sheet', '--code-bg']) {
      const r = ratio(p[role], skin.vars[ground]);
      assert.ok(r >= 3, `${id} ${role} ${p[role]} on ${ground}: ${r.toFixed(2)}`);
    }
  }
  const v = skin.vars;
  const dark = skin.scheme === 'dark';
  // Roles taken straight from a token keep its value when it already clears the floor.
  for (const [role, name] of [['keyword', '--teal'], ['tag', '--teal'], ['string', '--green'],
      ['added', '--green'], ['number', '--brass'], ['attribute', '--brass'], ['modified', '--brass'],
      ['invalid', '--red'], ['removed', '--red'], ['variable', '--ink'], ['operator', '--ink-2'],
      ['punctuation', '--ink-2']]) {
    assert.equal(p[role], v[name], `${id} ${role}`);
  }
  // Stepped roles move away from the ground: lighter on a dark skin, darker on paper.
  for (const [role, base] of [['type', p.keyword], ['function', p.number], ['regexp', p.string]]) {
    assert.notEqual(p[role], base, `${id} ${role}`);
    assert.ok(dark ? L(p[role]) > L(base) : L(p[role]) < L(base), `${id} ${role} ${p[role]} vs ${base}`);
  }
}
// Night's ink-3 clears the floor as it is; paper's is nudged darker until it does.
assert.equal(sdk.syntaxPalette(skins.night).comment, skins.night.vars['--ink-3']);
const paperComment = sdk.syntaxPalette(skins.paper).comment;
assert.ok(ratio(skins.paper.vars['--ink-3'], skins.paper.vars['--sheet']) < 3);
assert.ok(L(paperComment) < L(skins.paper.vars['--ink-3']));
// The placeholder (no tokens yet) has no palette: libraries keep their own until it arrives.
assert.equal(sdk.syntaxPalette({themeId:'unknown', scheme:'dark', known:false, vars:{}, fonts:{ui:'', mono:'', prose:''}}), null);
// A skin change gives a different palette.
assert.notDeepEqual(sdk.syntaxPalette(skins.night), sdk.syntaxPalette(skins['night-cool']));
''')


def test_built_runtime_syntax_palette_reads_user_skins_R05_W2b(tmp_path):
    """User skins may write any CSS colour per token and set scheme apart
    from the ground: the palette composites translucent inks over --sheet,
    falls back per role on a token it cannot read, and steps away from the
    ground itself, keeping every role at 3:1."""
    import json
    scheme, night = _SKIN_FIXTURES['night']
    base = {'themeId': 'user.test', 'scheme': scheme, 'known': True, 'vars': night,
            'fonts': {'ui': '', 'mono': '', 'prose': ''}}
    _run_built_runtime(tmp_path, 'const base = ' + json.dumps(base) + ''';
const lin = c => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const lum = h => [1, 3, 5].map(i => lin(parseInt(h.slice(i, i + 2), 16) / 255))
  .reduce((s, c, i) => s + c * [0.2126, 0.7152, 0.0722][i], 0);
const ratio = (x, y) => { const [a, b] = [lum(x), lum(y)].sort((p, q) => q - p); return (a + 0.05) / (b + 0.05); };
const skin = (vars, scheme = base.scheme) => ({...base, scheme, vars: {...base.vars, ...vars}});
const floor = (id, p, ground) => {
  for (const [role, hex] of Object.entries(p)) {
    assert.match(hex, /^#[0-9a-f]{6}$/, `${id} ${role}`);
    assert.ok(ratio(hex, ground) >= 3, `${id} ${role} ${hex} on ${ground}: ${ratio(hex, ground).toFixed(2)}`);
  }
};

// A translucent ink-3 is composited over the sheet, not read as opaque ink.
const alpha = sdk.syntaxPalette(skin({'--ink-3': 'rgba(226, 221, 212, 0.35)'}));
assert.ok(alpha);
assert.notEqual(alpha.comment, alpha.variable);
assert.notEqual(alpha.comment, '#e2ddd4');
assert.ok(lum(alpha.comment) < lum(alpha.variable));
floor('alpha', alpha, base.vars['--sheet']);

// A dark scheme on a light sheet: the direction comes from the ground, so
// every role still reads at 3:1 (none drifts to white).
const light = {'--sheet': '#f4f2ec', '--code-bg': '#ecebe5'};
const mixed = sdk.syntaxPalette(skin(light, 'dark'));
assert.ok(mixed);
floor('dark-on-light', mixed, '#f4f2ec');
floor('dark-on-light', mixed, '#ecebe5');
assert.ok(!Object.values(mixed).includes('#ffffff'));

// An hsl() hue the helper cannot read: its roles fall back to --ink; the
// palette stands and the other hues are untouched.
const hsl = sdk.syntaxPalette(skin({'--green': 'hsl(130 30% 60%)'}));
assert.ok(hsl);
assert.equal(hsl.string, base.vars['--ink']);
assert.equal(hsl.added, base.vars['--ink']);
assert.equal(hsl.keyword, base.vars['--teal']);
floor('hsl', hsl, base.vars['--sheet']);
// An unreadable ink-3 falls back to ink-2.
assert.equal(sdk.syntaxPalette(skin({'--ink-3': 'oklch(0.5 0 0)'})).comment, base.vars['--ink-2']);

// Only an unreadable --sheet or --ink leaves no palette.
assert.equal(sdk.syntaxPalette(skin({'--ink': 'oklch(0.9 0.01 80)'})), null);
assert.equal(sdk.syntaxPalette(skin({'--sheet': 'Canvas'})), null);
''')
