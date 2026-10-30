/** Canonical verifier-only prelude. Bundled ahead of time; no ambient authority. */
import { parse as yamlParse } from 'yaml';
import Ajv from 'ajv';
import { Parser } from 'htmlparser2';
export { checkRendererExport } from './renderer_shape';
export { verifyDefinition, definitionSchemas } from './definition_checks';

export type Verdict = {valid:true} | {valid:false,error:string};
export interface VerifyInput {
 path:string; text?:string; bytes?:{size:number,hash:string};
 previous_text?:string; previous_bytes?:{size:number,hash:string};
 package:Record<string,string | {size:number,hash:string}>; cell_paths:string[];
}
export interface BundleResult {source_hash:string; bundle_hash:string; code:string; entrypoint_js:string;}
declare global { var __collimate_bundle: (files:Record<string,string>,entrypoint:string)=>BundleResult; }
const bundleCapability=globalThis.__collimate_bundle;
export function bundle(files:Record<string,string>,entrypoint:string):BundleResult {
 return bundleCapability(files,entrypoint);
}
export const parseJson=(text:string):unknown=>JSON.parse(text);
export const parseYaml=(text:string):unknown=>yamlParse(text,{maxAliasCount:100,uniqueKeys:true});
export function validateSchema(schema:object,value:unknown):Verdict {
 const ajv=new Ajv({allErrors:true,strict:false,logger:false});
 const check=ajv.compile(schema);
 if(check.$async) throw Error('Verifier schemas must be synchronous');
 return check(value) ? {valid:true} : {valid:false,error:ajv.errorsText(check.errors,{separator:'; '})};
}
export interface HtmlToken {type:'open'|'close'|'text'|'comment'; name?:string; attributes?:Record<string,string>; text?:string;}
export function tokenizeHtml(text:string):HtmlToken[] {
 const tokens:HtmlToken[]=[];
 const parser=new Parser({onopentag:(name,attributes)=>tokens.push({type:'open',name,attributes}),
  onclosetag:name=>tokens.push({type:'close',name}), ontext:text=>tokens.push({type:'text',text}),
  oncomment:text=>tokens.push({type:'comment',text})},{decodeEntities:true});
 parser.write(text);parser.end();return tokens;
}
// Same literal-root/glob-component dialect as collimate.options_stack._glob.
export function matchGlob(path:string,pattern:string,root=''):boolean {
 if(!pattern || pattern.startsWith('/') || pattern.includes('\\') || pattern.includes('\0') || /^[A-Za-z]:/.test(pattern)) throw Error('invalid relative path');
 const parts:[string,boolean][]=root ? root.split('/').map(p=>[p,false]) : [];
 for(const part of pattern.split('/')) {
  if(part==='' || part==='.') continue;
  if(part==='..') { const last=parts[parts.length-1]; if(!last || (last[1] && /[*?]/.test(last[0]))) throw Error('path escapes cell or traverses a glob'); parts.pop(); }
  else parts.push([part,true]);
 }
 const escape=(s:string)=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
 let out='';
 parts.forEach(([part,glob],n)=>{
  if(!glob) out+=escape(part);
  else if(part==='**') {out+=n===parts.length-1 ? '.*' : '(?:[^/]+/)*';return;}
  else { for(let i=0;i<part.length;i++) {
   if(part.slice(i,i+2)==='**') {out+='.*';i++;}
   else if(part[i]==='*') out+='[^/]*';else if(part[i]==='?') out+='[^/]';else out+=escape(part[i]);
  }}
  if(n!==parts.length-1) out+='/';
 });
 return new RegExp('^'+out+'$').test(path);
}
export { default as rendererSchemas } from './renderer-schemas.json';
