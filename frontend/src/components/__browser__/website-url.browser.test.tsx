import {it,expect,vi} from 'vitest';
import {render} from 'vitest-browser-react';
import {page} from 'vitest/browser';
import {NativeRenderer} from '../NativeRenderer';
const guest=vi.hoisted(()=>({native:true,mount:vi.fn(),loadURL:vi.fn(async()=>{}),on:()=>()=>{},destroy:()=>{},setConcealed:()=>{}}));
vi.mock('@/lib/api',async original=>({...await original<typeof import('@/lib/api')>(),getApiUrl:()=>`${window.location.origin}/api`}));
vi.mock('@/lib/desktop',async original=>({...await original<typeof import('@/lib/desktop')>(),isDesktop:()=>guest.native,hasCap:()=>guest.native,desktopCaps:()=>({nativeGuests:guest.native,directLoopback:guest.native})}));
vi.mock('../guestMount',async original=>({...await original<typeof import('../guestMount')>(),mountGuestInto:(...args:unknown[])=>{guest.mount(...args);return guest;}}));

it.each([true,false])('keeps native navigation independent of the saved browser pointer (native=%s) __F1742',async native=>{
 guest.native=native;guest.mount.mockClear();guest.loadURL.mockClear();
 const pointer={url:'/proxy/browser',directUrl:'http://127.0.0.1:6100',buttons:[],loadingCover:false};
 const writes:string[]=[];
 const props={rendererId:'fixture-website-url',rendererHash:'website-url',fileName:'site.website',fileContent:JSON.stringify(pointer),isActiveFocus:true,onContentChange:(body:string)=>writes.push(body)};
 let view=await render(<NativeRenderer {...props}/>);
 try{
  await expect.element(page.getByPlaceholder('https://...')).toHaveValue(native?pointer.directUrl:pointer.url);
  await page.getByPlaceholder('https://...').fill('http://127.0.0.1:6101/next');await page.getByRole('button',{name:'Go',exact:true}).click();
  if(native){await vi.waitFor(()=>expect(guest.loadURL).toHaveBeenCalledWith('http://127.0.0.1:6101/next'));expect(writes).toEqual([]);}
  else expect(JSON.parse(writes.at(-1)!)).toMatchObject({...pointer,url:'http://127.0.0.1:6101/next'});
  await view.unmount();view=await render(<NativeRenderer {...props}/>);
  await expect.element(page.getByPlaceholder('https://...')).toHaveValue(native?pointer.directUrl:pointer.url);
  if(native){
   await view.rerender(<NativeRenderer {...props} fileContent={JSON.stringify({...pointer,directUrl:'http://127.0.0.1:6102'})}/>);
   await expect.element(page.getByPlaceholder('https://...')).toHaveValue('http://127.0.0.1:6102');
   expect(guest.mount.mock.calls.at(-1)?.[2]).toMatchObject({src:'http://127.0.0.1:6102'});
  }
 }finally{await view.unmount();}
},30000);
