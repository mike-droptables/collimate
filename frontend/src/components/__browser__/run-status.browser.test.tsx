import {render} from 'vitest-browser-react';
import {expect,it} from 'vitest';
import '@/app/globals.css';
import {RunStatusIndicator} from '../RunStatusIndicator';
import {useCollimateStore} from '@/stores/useCollimateStore';
import type {Channel} from '@/lib/types';
import type {ActiveRunInfo} from '@/lib/api';

const run=(id:string,runner:string):ActiveRunInfo=>({run_id:id,cell_id:'cell',cell_name:'A cell with a long name that has to be cut short',runner_id:runner,runner_name:runner,
 label:`${id}-label`,folder:`_runs/${id}-label`,generator_name:'synthetic',started_at:new Date(Date.now()-90*60_000).toISOString()});

it('the running status paints teal on the skin, and its menu stays between 300 and 360px inside the viewport (C04)',async()=>{
 const channel:Channel={id:'11111111-1111-4111-8111-111111111111',name:'Status browser',main_stacks:[],nav_stacks:[],main_selection_id:null,nav_selection_id:null};
 const prior=useCollimateStore.getState();
 useCollimateStore.setState({activeRunsEverywhere:[
  {channel:'11111111-1111-4111-8111-111111111111',channel_name:'Status browser',runs:[run('a','a runner whose name goes on and on and on and on')]},
  {channel:'22222222-2222-4222-8222-222222222222',channel_name:'Status browser',runs:[run('b','b')]},
 ],stoppingRunIds:new Set()});
 const view=await render(<RunStatusIndicator activeChannel={channel} isZoomedOut={false} onExitZoom={()=>{}} onSelectChannel={()=>{}} />);
 try{
  const status=document.querySelector<HTMLButtonElement>('button[aria-haspopup="dialog"]')!;
  expect(status.textContent).toBe('1 running(1)');
  const dot=status.querySelector('i')!;
  expect(getComputedStyle(dot).backgroundColor).toBe(getComputedStyle(status).color);
  expect(getComputedStyle(dot).backgroundColor).not.toBe('rgba(0, 0, 0, 0)');
  status.click();
  await expect.poll(()=>document.querySelector('[role="dialog"][aria-label="Running"]')!==null).toBe(true);
  const menu=document.querySelector<HTMLElement>('[role="dialog"][aria-label="Running"]')!;
  await expect.poll(()=>menu.getBoundingClientRect().left>=0).toBe(true);
  const box=menu.getBoundingClientRect();
  expect(box.width).toBeGreaterThanOrEqual(300);
  expect(box.width).toBeLessThanOrEqual(360);
  expect(box.right).toBeLessThanOrEqual(window.innerWidth);
  expect(menu.textContent).toContain('1h 30m');
  // The menu's rows: channel, then cell, then run; a live run's dot is teal too.
  expect(menu.querySelectorAll('[data-testid="runs-menu-channel"]')).toHaveLength(2);
  const runDot=menu.querySelector<HTMLElement>('[data-testid="runs-menu-run"] i')!;
  expect(getComputedStyle(runDot).backgroundColor).toBe(getComputedStyle(status).color);
  for(const row of menu.querySelectorAll<HTMLElement>('[data-run]'))expect(row.getBoundingClientRect().right).toBeLessThanOrEqual(box.right+0.5);
 }finally{await view.unmount();useCollimateStore.setState(prior);}
});
