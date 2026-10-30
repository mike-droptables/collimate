import {getApiUrl} from './api';

/** A run's surface: `starting` from the run's acceptance until its
 *  generator registers it (chat_id, ws_path and token are empty), then
 *  `live`. */
export interface SurfaceSession {
  chat_id:string; ws_path:string; token:string; label:string;
  run_id:string; state:'starting'|'live';
}

/** Live coordinates belong to the mounted path; history never joins a live run. */
export async function rendererSurface(channel:string,cellId:string,path:string,versionId?:string):Promise<SurfaceSession|null>{
  if(versionId || !channel || !cellId)return null;
  const encoded=path.split('/').map(encodeURIComponent).join('/');
  const response=await fetch(`${getApiUrl()}/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/surfaces/${encoded}`,{cache:'no-store'});
  if(response.status===404)return null;
  if(!response.ok)throw new Error(`Surface lookup: HTTP ${response.status}`);
  const value=await response.json();
  if(!value || !['chat_id','ws_path','token','label','run_id'].every(key=>typeof value[key]==='string')
      || (value.state!=='starting' && value.state!=='live'))
    throw new Error('Invalid surface response');
  return value;
}

export function subscribeSurface(channel:string,cellId:string,path:string,callback:()=>void):()=>void{
  const changed=(event:Event)=>{
    const detail=(event as CustomEvent).detail;
    if(detail?.channel===channel && detail.cellId===cellId && detail.path===path)callback();
  };
  window.addEventListener('collimate:surface-changed',changed);
  return()=>window.removeEventListener('collimate:surface-changed',changed);
}
