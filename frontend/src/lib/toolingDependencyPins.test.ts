import {readFileSync} from 'node:fs';
import {expect,it} from 'vitest';

it('the lock carries no optional Sass, and brace expansion paths avoid the audited affected ranges __P3F06',()=>{
 const lock=JSON.parse(readFileSync('package-lock.json','utf8')) as {packages:Record<string,{version?:string}>};
 // Sass, like terser and yaml, is only an optional vite peer: never installed.
 expect(lock.packages['node_modules/sass']).toBeUndefined();
 expect(lock.packages['node_modules/braces']).toBeUndefined();
 expect(lock.packages['node_modules/chokidar']?.version?.startsWith('3.')).not.toBe(true);
 const versions=Object.entries(lock.packages).filter(([path])=>path.endsWith('/brace-expansion')).map(([,pkg])=>pkg.version!);
 expect(versions.length).toBeGreaterThan(0);
 for(const version of versions){
  const [major,minor,patch]=version.split('.').map(Number);
  const safe=major===1 ? minor>1||minor===1&&patch>=21
   : major===2 ? minor>1||minor===1&&patch>=7
   : major===5 ? minor>0||patch>=12 : major>5;
  expect(safe,version).toBe(true);
 }
});
