import {useSyncExternalStore} from 'react';
import {caseFold} from './unicodeCaseFold';
import {handlerTypeForPath,handlerTypeRules,type HandlerTypes} from './handlerTypes';
export type HandlerRendering = HandlerTypes;
type Rule = {ext:string;renderAs:string|null};
let rules:Rule[]=[];
const listeners=new Set<()=>void>();
const subscribe=(cb:()=>void)=>{listeners.add(cb);return()=>{listeners.delete(cb);};};
export function setRendererHandlers(handlers:HandlerRendering[]):void {
  const next=handlerTypeRules(handlers).map(({ext,renderAs})=>({ext,renderAs}));
  if(JSON.stringify(next)===JSON.stringify(rules))return;
  rules=next;for(const listener of listeners)listener();
}
/** Includes inert/clashing suffixes so a shorter alias cannot steal their files. */
export function handlerExtensions():string[]{return rules.map(rule=>rule.ext);}
export function rendererFilename(real:string):string {
  const rule=handlerTypeForPath(rules,real);
  if(!rule?.renderAs)return real;
  let offset=real.length,length=0;
  const chars=Array.from(real);
  for(let i=chars.length-1;i>=0&&length<rule.ext.length;i--){offset-=chars[i].length;length+=caseFold(chars[i]).length;}
  return real.slice(0,offset)+rule.renderAs;
}
export function useRendererFilename(real:string):string {
  return useSyncExternalStore(subscribe,()=>rendererFilename(real),()=>rendererFilename(real));
}
export function rendererRealPath(real:string,display:string,path:string):string {
  return path===display?real:path;
}
export function rendererChangedFiles(real:string,display:string,paths:string[]):string[]{
  return paths.map(path=>path===real?display:path);
}
