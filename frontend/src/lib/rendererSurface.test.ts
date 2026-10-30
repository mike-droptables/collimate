// @vitest-environment jsdom
import {afterEach,expect,it,vi} from 'vitest';
vi.mock('./api',()=>({getApiUrl:()=>'/api'}));
import {rendererSurface,subscribeSurface} from './rendererSurface';
afterEach(()=>vi.unstubAllGlobals());
it('looks up the mounted surface, keeps history offline and distinguishes transport failure (D22)',async()=>{
 const fetcher=vi.fn();vi.stubGlobal('fetch',fetcher);
 expect(await rendererSurface('channel','cell','folder/file.panel','old-version')).toBeNull();
 expect(fetcher).not.toHaveBeenCalled();
 fetcher.mockResolvedValueOnce({status:404});
 expect(await rendererSurface('channel','cell','folder/file.panel')).toBeNull();
 expect(fetcher.mock.calls[0][0]).toBe('/api/channels/channel/cells/cell/surfaces/folder/file.panel');
 fetcher.mockResolvedValueOnce({status:503,ok:false});
 await expect(rendererSurface('channel','cell','folder/file.panel')).rejects.toThrow('503');
 const session={chat_id:'chat',ws_path:'/ws',token:'token',label:'Review',run_id:'run',state:'live'};
 fetcher.mockResolvedValueOnce({status:200,ok:true,json:async()=>session});
 expect(await rendererSurface('channel','cell','folder/file.panel')).toEqual(session);
});
it('accepts a starting surface and refuses an unknown state (GN-03)',async()=>{
 const fetcher=vi.fn();vi.stubGlobal('fetch',fetcher);
 const starting={state:'starting',run_id:'run',label:'Agent',chat_id:'',ws_path:'',token:''};
 fetcher.mockResolvedValueOnce({status:200,ok:true,json:async()=>starting});
 expect(await rendererSurface('channel','cell','status.chat')).toEqual(starting);
 fetcher.mockResolvedValueOnce({status:200,ok:true,json:async()=>({...starting,state:'pending'})});
 await expect(rendererSurface('channel','cell','status.chat')).rejects.toThrow('Invalid surface response');
 fetcher.mockResolvedValueOnce({status:200,ok:true,json:async()=>{const {state:_,...rest}=starting;return rest;}});
 await expect(rendererSurface('channel','cell','status.chat')).rejects.toThrow('Invalid surface response');
});
it('notifies only matching channel, cell and path and removes subscriptions (D22)',()=>{
 const callback=vi.fn();const off=subscribeSurface('channel','cell','view.panel',callback);
 const notify=(detail:unknown)=>window.dispatchEvent(new CustomEvent('collimate:surface-changed',{detail}));
 notify({channel:'other',cellId:'cell',path:'view.panel'});
 notify({channel:'channel',cellId:'other',path:'view.panel'});
 notify({channel:'channel',cellId:'cell',path:'other.panel'});
 expect(callback).not.toHaveBeenCalled();
 notify({channel:'channel',cellId:'cell',path:'view.panel'});
 expect(callback).toHaveBeenCalledOnce();off();
 notify({channel:'channel',cellId:'cell',path:'view.panel'});
 expect(callback).toHaveBeenCalledOnce();
});
