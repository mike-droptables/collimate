// DOM-free snapshot codec for the exact pinned Crepe feature graph.
// Public Milkdown schema/serializer plugins are reused; no editor view mounts.
// The math_inline and LaTeX code-block recipes below are adapted from
// @milkdown/crepe 7.22.3 src/feature/latex/{inline-latex,block-latex}.ts:
/*!
The MIT License (MIT)

Copyright (c) 2020-present Mirone

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/
import { restoreProseMirrorSnapshot } from './snapshot-model.ts';
import { extendImageBlockSchema, extendImageSchema } from './embed-markdown.ts';
import { Clock, Container, Ctx } from '@milkdown/kit/ctx';
import { config, init, schema, schemaCtx, remarkCtx, editorViewCtx } from '@milkdown/kit/core';
import { SerializerState } from '@milkdown/kit/transformer';
import { schema as commonmarkSchema, codeBlockSchema, imageSchema, remarkPreserveEmptyLinePlugin } from '@milkdown/kit/preset/commonmark';
import { schema as gfmSchema, remarkGFMPlugin } from '@milkdown/kit/preset/gfm';
import { imageBlockSchema } from '@milkdown/kit/component/image-block';
import { $nodeSchema, $remark } from '@milkdown/kit/utils';
import remarkMath from 'remark-math';
const mathInlineSchema=$nodeSchema('math_inline',()=>({
 group:'inline',inline:true,draggable:true,atom:true,attrs:{value:{default:''}},
 toMarkdown:{match:node=>node.type.name==='math_inline',runner:(state,node)=>state.addNode('inlineMath',undefined,node.attrs.value)},
}));
const blockLatexSchema=codeBlockSchema.extendSchema(prev=>ctx=>{
 const base=prev(ctx);return {...base,toMarkdown:{match:base.toMarkdown.match,runner:(state,node)=>{
  if((node.attrs.language??'').toLowerCase()==='latex')state.addNode('math',undefined,node.content.firstChild?.text||'');
  else base.toMarkdown.runner(state,node);
 }}};
});
const math=$remark('remarkMath',()=>remarkMath);
// The editor's embeds: both image nodes with a width and an align, the block
// with its real alt (embed-markdown.ts, shared with the renderer).
const embedImageSchema=imageSchema.extendSchema(extendImageSchema);
const embedImageBlockSchema=imageBlockSchema.extendSchema(extendImageBlockSchema);
const ctx=new Ctx(new Container(),new Clock());
// Paragraph serialization consults only the current document lastChild.
// Supply that context, never a DOM EditorView.
ctx.inject(editorViewCtx, {state:{doc:null}});
const plugins=[schema,init(null),config(()=>{}),...commonmarkSchema,...gfmSchema,...imageBlockSchema,...embedImageSchema,...embedImageBlockSchema,...mathInlineSchema,...blockLatexSchema,...remarkGFMPlugin,...remarkPreserveEmptyLinePlugin,...math];
const handlers=plugins.map(plugin=>plugin(ctx));
const ready=Promise.all(handlers.map(handler=>handler()));
// The caller receives initialization failures on its first materialization.
void ready.catch(()=>undefined);
self.onmessage=async event=>{
 const {type,requestId,payload}=event.data;
 if(type!=='materialize')return;
 try {
  await ready;
  const documentSchema=ctx.get(schemaCtx);
  const doc=restoreProseMirrorSnapshot(documentSchema,payload);
  ctx.set(editorViewCtx,{state:{doc}});
  // A rejected document must not leave a partially traversed serializer stack
  // in the next job. Schema and remark configuration remain shared and pinned.
  const body=SerializerState.create(documentSchema,ctx.get(remarkCtx))(doc);
  self.postMessage({type:'materialized',requestId,body});
 }
 catch(error){self.postMessage({type:'error',requestId,error:String(error)});}
};
