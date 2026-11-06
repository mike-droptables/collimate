import {it,expect} from 'vitest';
import '@/app/globals.css';

it('Tailwind dark variant follows data-scheme rather than fixed dark class __F0962 (S01)',()=>{
 const root=document.documentElement;const prev=root.getAttribute('data-scheme');const previousClass=root.className;
 const probe=document.createElement('div');probe.className='bg-sheet dark:bg-canvas';document.body.append(probe);
 try {
  root.style.setProperty('--sheet','rgb(1, 2, 3)');root.style.setProperty('--canvas','rgb(4, 5, 6)');
  root.classList.add('dark');root.setAttribute('data-scheme','light');
  expect(getComputedStyle(probe).backgroundColor).toBe('rgb(1, 2, 3)');
  root.setAttribute('data-scheme','dark');expect(getComputedStyle(probe).backgroundColor).toBe('rgb(4, 5, 6)');
 }finally{probe.remove();root.className=previousClass;root.style.removeProperty('--sheet');root.style.removeProperty('--canvas');if(prev===null)root.removeAttribute('data-scheme');else root.setAttribute('data-scheme',prev);}
});
