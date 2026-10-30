import {readFileSync} from 'node:fs';
import {expect,it} from 'vitest';
import {MIRRORED_HOSTS,mirrorUrl} from './moduleMirror';

const contract=JSON.parse(readFileSync('../sdk/src/collimate/module_mirror_contract.json','utf8')) as {upstreams:string[]};
const indexHtml=readFileSync('index.html','utf8');
const sandboxHtml=readFileSync('public/sandbox.html','utf8');
const upstreamMap=(base:string)=>Object.fromEntries([...MIRRORED_HOSTS].map(host=>[`https://${host}/`,`${base}${host}/`]));

it('mirrors exactly the SDK contract upstreams',()=>{
  expect([...MIRRORED_HOSTS].map(host=>`https://${host}/`)).toEqual(contract.upstreams);
});

it('maps both module CDNs under the API base, keeping the path and query as written',()=>{
  expect(mirrorUrl('https://esm.sh/react@19.2.0?target=es2022','../api')).toBe('../api/mirror/esm.sh/react@19.2.0?target=es2022');
  expect(mirrorUrl('https://cdn.jsdelivr.net/npm/katex@0.19.1/dist/katex.min.css','http://app.test/api/')).toBe('http://app.test/api/mirror/cdn.jsdelivr.net/npm/katex@0.19.1/dist/katex.min.css');
  expect(mirrorUrl('https://esm.sh/@scope/pkg@%5E1.0.0/x.mjs?deps=a@1.0.0,b@2.0.0','/api')).toBe('/api/mirror/esm.sh/@scope/pkg@%5E1.0.0/x.mjs?deps=a@1.0.0,b@2.0.0');
  expect(mirrorUrl('https://ESM.sh:443/a@1.0.0','/api')).toBe('/api/mirror/esm.sh/a@1.0.0');
});

it('leaves every other URL unchanged',()=>{
  for(const url of ['https://unpkg.com/demo@1.2.3/module.js','http://esm.sh/react@19.2.0','https://user:pass@esm.sh/react@19.2.0',
    'https://esm.sh.evil.example/react@19.2.0','data:text/css,a{}','blob:http://app.test/0000','/api/sdk/react.js','not a url'])
    expect(mirrorUrl(url,'/api')).toBe(url);
});

it('index.html opens <head> with the default mirror import map, before any module script',()=>{
  const head=/<head>\s*<script type="importmap" id="collimate-mirror-map">([^<]*)<\/script>/.exec(indexHtml);
  expect(head).not.toBeNull();
  expect(JSON.parse(head![1])).toEqual({imports:upstreamMap('../api/mirror/')});
  expect(indexHtml.indexOf('id="collimate-mirror-map"')).toBeLessThan(indexHtml.indexOf('type="module"'));
});

/** Run the sandbox's first head script against a document at `href`; answer the import map it adds. */
function sandboxMap(href:string):{map:unknown;base:unknown;hosts:unknown;first:boolean} {
  const first=/<head>\s*<script>([\s\S]*?)<\/script>/.exec(sandboxHtml);
  if(!first)return {map:null,base:null,hosts:null,first:false};
  let added:{type:string;textContent:string}|null=null;
  const window:Record<string,unknown>={};
  const document={createElement:()=>({type:'',textContent:''}),currentScript:{after:(node:{type:string;textContent:string})=>{added=node;}}};
  new Function('location','document','window',first[1])(new URL(href),document,window);
  const map=added as {type:string;textContent:string}|null;
  return {map:map?.type==='importmap'?JSON.parse(map.textContent):null,base:window.__collimateMirrorBase,hosts:window.__collimateMirrorHosts,first:true};
}

it('sandbox.html opens <head> with the script that adds its mirror import map from the apiBase param',()=>{
  const hosts=[...MIRRORED_HOSTS];
  expect(sandboxMap('http://app.test/sandbox.html?renderer=r&apiBase=')).toEqual({first:true,base:'http://app.test/api/mirror/',hosts,map:{imports:upstreamMap('http://app.test/api/mirror/')}});
  expect(sandboxMap('http://app.test/ui/sandbox.html?apiBase=..')).toEqual({first:true,base:'http://app.test/api/mirror/',hosts,map:{imports:upstreamMap('http://app.test/api/mirror/')}});
  expect(sandboxMap('http://app.test/sandbox.html?apiBase=http%3A%2F%2Fapi.test%3A8000%2F').map).toEqual({imports:upstreamMap('http://api.test:8000/api/mirror/')});
});

it('neither document preloads, preconnects to or links a module CDN',()=>{
  for(const html of [indexHtml,sandboxHtml]){
    for(const tag of html.match(/<link\b[^>]*>/g)??[])expect(tag).not.toMatch(/esm\.sh|cdn\.jsdelivr\.net|unpkg\.com/);
    expect(html).not.toContain('modulepreload');
  }
});
