#!/usr/bin/env node
// Build only trusted SDK prelude dependencies. Authored verifiers never resolve npm.
//
//   node scripts/build_verify_prelude.mjs            write prelude.js, the licenses and the manifest
//   node scripts/build_verify_prelude.mjs --outputs  write prelude.js only (scripts/build_outputs.py)
//   node scripts/build_verify_prelude.mjs --check    rebuild and compare: the licenses, the manifest
//                                                    and prelude.js when it is built
//
// shared/verify/prelude.js is git-ignored; the committed manifest beside it,
// prelude.json, pins its sha256 and its sources' (the shape of
// shared/renderer-runtime/index.json).
import { build, version } from 'esbuild';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const mode=process.argv.includes('--check')?'check':process.argv.includes('--outputs')?'outputs':'write';
const contract=JSON.parse(await readFile(path.join(root,'src/collimate/renderer_bundle_contract.json'),'utf8'));
if(version!==contract.esbuild_version) throw Error('Prelude compiler version differs from canonical pin');
const result=await build({absWorkingDir:root,write:false,entryPoints:[path.join(root,'shared/verify/prelude.ts')],outfile:path.join(root,'shared/verify/prelude.js'),
 bundle:true,format:'esm',platform:'neutral',target:'es2022',conditions:['browser'],mainFields:['browser','module','main'],
 nodePaths:[path.join(root,'scripts/node_modules')],preserveSymlinks:true,legalComments:'none',tsconfigRaw:{}});
// preserveSymlinks: the bundle's path comments name scripts/node_modules as it is
// reached, so a symlinked install builds the same bytes as a copied one.
// Bundle must be self-contained: the native verifier resolver has one module only.
const code=result.outputFiles[0].text;
if(/^import\s/m.test(code)) throw Error('Prelude still contains runtime imports');
const names=new Set();
async function visit(name){
 if(names.has(name))return;names.add(name);
 const packageDir=path.join(root,'scripts/node_modules',name);
 const pkg=JSON.parse(await readFile(path.join(packageDir,'package.json'),'utf8'));
 for(const dep of Object.keys(pkg.dependencies||{}))await visit(dep);
}
for(const name of ['yaml','ajv','htmlparser2'])await visit(name);
let licenses='Pinned dependencies bundled into the canonical verifier prelude.\n';
for(const name of [...names].sort()){
 const dir=path.join(root,'scripts/node_modules',name);
 const pkg=JSON.parse(await readFile(path.join(dir,'package.json'),'utf8'));
 let license;for(const filename of ['LICENSE','LICENSE.md','LICENSE-MIT.txt','LICENSE.txt','license']){
  try{license=await readFile(path.join(dir,filename),'utf8');break;}catch{}
 }
 if(!license)throw Error('Missing dependency license: '+name);
 licenses+='\n=== '+name+'@'+pkg.version+' ===\n'+license+'\n';
}
const hash=text=>createHash('sha256').update(text).digest('hex');
const inputs=['scripts/build_verify_prelude.mjs','scripts/package-lock.json','scripts/package.json','shared/verify/prelude.ts','src/collimate/renderer_bundle_contract.json'];
const manifest={schema:1,compiler:version,inputs:{},outputs:{'prelude.js':hash(code),'dependency-licenses.txt':hash(licenses)}};
for(const input of inputs.sort())manifest.inputs[input]=hash(await readFile(path.join(root,input)));
const target=name=>path.join(root,'shared/verify',name);
// Committed: the licenses and the manifest. Built and git-ignored: prelude.js.
const committed={'dependency-licenses.txt':licenses,'prelude.json':JSON.stringify(manifest,null,2)+'\n'};
if(mode==='check'){
 for(const [name,text] of Object.entries(committed)){
  if(await readFile(target(name),'utf8').catch(()=>null)!==text)throw Error('Canonical verifier build differs: '+name);
 }
 const existing=await readFile(target('prelude.js'),'utf8').catch(error=>{if(error.code==='ENOENT')return code;throw error;});
 if(existing!==code)throw Error('Built prelude.js differs from its sources (rebuild it: node scripts/build_verify_prelude.mjs --outputs)');
}else{
 await writeFile(target('prelude.js'),code);
 if(mode==='write')for(const [name,text] of Object.entries(committed))await writeFile(target(name),text);
}
