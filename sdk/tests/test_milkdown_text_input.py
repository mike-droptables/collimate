"""Ordinary input uses public PM semantics while browser composition stays native."""
import subprocess
from pathlib import Path

from tests._workspace import RENDERERS

ROOT = Path(__file__).resolve().parents[1]


def test_text_input_preserves_selection_history_marks_rules_and_composition(tmp_path):
    target = tmp_path / 'text-input.mjs'
    build = subprocess.run(['scripts/node_modules/esbuild/bin/esbuild',
        str(RENDERERS / 'documents/milkdown/text-input.ts'), '--format=esm', f'--outfile={target}'],
        cwd=ROOT, capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    program = r'''
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(new URL('./scripts/package.json',`file://${process.cwd()}/`));
const {Schema}=require('prosemirror-model');
const {EditorState,TextSelection,NodeSelection}=require('prosemirror-state');
const {history,undo,redo}=require('prosemirror-history');
const {transactionTextInput}=await import(process.argv[1]);
const handle=transactionTextInput(TextSelection);
const schema=new Schema({nodes:{doc:{content:'paragraph+'},paragraph:{content:'text*',group:'block'},text:{group:'inline'}},marks:{strong:{}}});
const paragraph=text=>schema.node('paragraph',null,text?schema.text(text):null);
const make=(text='hello')=>({state:EditorState.create({schema,doc:schema.node('doc',null,[paragraph(text)]),plugins:[history()]}),editable:true,composing:false,
 dispatch(tr){this.state=this.state.apply(tr)},someProp(){return false}});
const input=(data='x',extra={})=>({inputType:'insertText',data,cancelable:true,defaultPrevented:false,isComposing:false,preventDefault(){this.defaultPrevented=true},...extra});
const view=make();view.dispatch(view.state.tr.setSelection(TextSelection.create(view.state.doc,2,5)));
assert.equal(handle(view,input('🌻')),true);assert.equal(view.state.doc.textContent,'h🌻o');assert.equal(view.state.selection.from,4);
assert.equal(undo(view.state,tr=>view.dispatch(tr)),true);assert.equal(view.state.doc.textContent,'hello');assert.equal(view.state.selection.from,2);assert.equal(view.state.selection.to,5);
assert.equal(redo(view.state,tr=>view.dispatch(tr)),true);assert.equal(view.state.doc.textContent,'h🌻o');
const marked=make('');marked.dispatch(marked.state.tr.addStoredMark(schema.marks.strong.create()));handle(marked,input('bold'));
assert.equal(marked.state.doc.firstChild.firstChild.marks[0].type.name,'strong');
const rules=make('');let calls=0;rules.someProp=(name,invoke)=>{assert.equal(name,'handleTextInput');return invoke((v,from,to,text,deflt)=>{calls++;assert.equal(text,' ');const tr=deflt();assert.equal(tr.doc.textContent,' ');v.dispatch(v.state.tr.insertText('rule',from,to));return true})};
const event=input(' ');assert.equal(handle(rules,event),true);assert.equal(calls,1);assert.equal(rules.state.doc.textContent,'rule');assert.equal(event.defaultPrevented,true);
for(const extra of [{isComposing:true},{inputType:'insertCompositionText'},{inputType:'insertReplacementText'},{inputType:'insertFromPaste'},{inputType:'historyUndo'},{cancelable:false},{defaultPrevented:true},{data:null},{data:'\n'}]){
 const native=make();const before=native.state;assert.equal(handle(native,input('x',extra)),false);assert.equal(native.state,before);
}
for(const extra of [{composing:true},{editable:false}]){const native=Object.assign(make(),extra);assert.equal(handle(native,input()),false);assert.equal(native.state.doc.textContent,'hello')}
const cross=make();cross.dispatch(cross.state.tr.insert(cross.state.doc.content.size,paragraph('world')));cross.dispatch(cross.state.tr.setSelection(TextSelection.create(cross.state.doc,2,9)));assert.equal(handle(cross,input()),false);
const node=make();node.dispatch(node.state.tr.setSelection(NodeSelection.create(node.state.doc,0)));assert.equal(handle(node,input()),false);
console.log('selection, history, marks, input rules, and native composition routing passed');
'''
    result = subprocess.run(['node', '--input-type=module', '-e', program, target.as_uri()],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
