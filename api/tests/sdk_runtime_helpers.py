"""Execute actual compiled SDK modules with deterministic hook primitives."""
import json
import subprocess

from collimate.shared import toolchain


def run_runtime(client, tmp_path, assertions, *, render_components=False):
    react = tmp_path/'react.mjs'
    react.write_text('''export const effects=[],cleanups=[],states=[];
export function createContext(value){return {value};}
export function useContext(context){return context.value;}
export function useState(initial){const index=states.length;states.push(typeof initial==='function'?initial():initial);return [states[index],value=>{states[index]=value;}];}
export function useEffect(fn){effects.push(fn);cleanups.push(fn());}
export function useLayoutEffect(fn){effects.push(fn);cleanups.push(fn());}
export function useCallback(fn){return fn;}
export function useMemo(fn){return fn();}
export function useRef(value){return {current:value};}
export default {Component:class {},createElement(){}};
''')
    dom = tmp_path/'dom.mjs'
    dom.write_text('export function createRoot(){throw Error("mount needs browser gate")}')
    if render_components:
        # Execute the built platform's component/context callbacks; this is
        # a deterministic tree walker, not a browser or DOM rendering claim.
        react.write_text(react.read_text().replace(
            'return {value};', 'const context={value};context.Provider={context};return context;'
        ).replace('export default {Component:class {},createElement(){}};', '''
export default {Component:class {constructor(props){this.props=props;}},
 createElement(type,props,...children){return {type,props:{...props,children}};}};
'''))
        dom.write_text('import {cleanups} from ' + json.dumps(react.as_uri()) + ''';
function walk(node){
 if(node==null)return;
 if(Array.isArray(node)){for(const child of node)walk(child);return;}
 if(node.type?.context){node.type.context.value=node.props.value;walk(node.props.children);return;}
 if(typeof node.type==='function'){
  walk(node.type.prototype?.render?new node.type(node.props).render():node.type(node.props));return;}
 walk(node.props?.children);
}
export function createRoot(){return {render:walk,unmount(){for(const cleanup of cleanups)cleanup?.();}};}
''')
    response = client.get('/api/sdk/react.js')
    assert response.status_code == 200, response.text
    pins = toolchain()
    body = (response.text
            .replace(f"https://esm.sh/react-dom@{pins['react_dom']}/client?deps=react@{pins['react']}&target=es2022", dom.as_uri())
            .replace(f"https://esm.sh/react@{pins['react']}?target=es2022", react.as_uri()))
    module = tmp_path/'runtime.mjs'
    module.write_text(body)
    script = tmp_path/'run.mjs'
    script.write_text("import assert from 'node:assert/strict';\nimport * as hooks from " + json.dumps(react.as_uri()) + ";\nimport * as sdk from " + json.dumps(module.as_uri()) + ";\n" + assertions)
    result = subprocess.run(['node',str(script)],capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
