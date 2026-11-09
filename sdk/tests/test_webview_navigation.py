"""Execute the actual native-view address callbacks with a controlled guest."""
from pathlib import Path
import subprocess

from tests._workspace import BUILTINS


def test_refused_guest_navigation_keeps_typed_address_after_blur__F1737(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    source = (BUILTINS / 'live/webview/main.tsx').read_text()
    start = source.index('    const commitUrl = () => {')
    end = source.index('\n\n    return (', start)
    callbacks = source[start:end]
    module = tmp_path / 'navigation.ts'
    module.write_text('''export function controls(bindings: any) {
 const {handleRef,editingUrlRef,liveUrlRef,urlInput,setUrlInput,setNavigationError,rejectedUrlRef,navigationSeqRef}=bindings;
 const withScheme=(url: string)=>url.includes('://')?url:'https://'+url;
 const displayUrl=(url: string)=>url;
 const quiet=(promise: Promise<unknown>|undefined)=>promise?.catch(()=>{});
''' + callbacks + '\nreturn {commitUrl,onFocusUrl,onBlurUrl};}\n')
    built = tmp_path / 'navigation.mjs'
    compile_result = subprocess.run([str(repo / 'scripts/node_modules/esbuild/bin/esbuild'),
                                    str(module), '--format=esm', f'--outfile={built}'],
                                   capture_output=True, text=True)
    assert compile_result.returncode == 0, compile_result.stderr
    script = tmp_path / 'run.mjs'
    script.write_text('''import assert from 'node:assert/strict';
const {controls}=await import(process.argv[2]);
const values={url:'new.invalid/path',error:''};
const bindings={urlInput:values.url,handleRef:{current:{loadURL:async()=>{throw new Error('navigation denied');}}},editingUrlRef:{current:true},liveUrlRef:{current:'https://old.example/'},rejectedUrlRef:{current:null},navigationSeqRef:{current:0},setUrlInput:value=>values.url=value,setNavigationError:value=>values.error=value};
const address=controls(bindings);address.commitUrl();address.onBlurUrl();await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(values.url,'new.invalid/path','blur must not lose the refused typed address');assert.match(values.error,/navigation denied/);
bindings.handleRef.current={loadURL:async()=>{}};address.commitUrl();await new Promise(resolve=>setTimeout(resolve,0));assert.equal(values.error,'');
''')
    result = subprocess.run(['node', str(script), built.as_uri()], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
