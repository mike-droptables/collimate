/** Same framed identity as collimate._host.package_hash; digest is pinned SHA256. */
export function contentHash(files, digest) {
  const utf8=new TextEncoder();
  const parts=[utf8.encode('collimate-package-approval-v1\0')];
  const names=Object.keys(files).map(path=>[path,utf8.encode(path)]);
  names.sort((a,b)=>{for(let i=0;i<Math.min(a[1].length,b[1].length);i++)if(a[1][i]!==b[1][i])return a[1][i]-b[1][i];return a[1].length-b[1].length;});
  for(const [path,name] of names){const size=new Uint8Array(8);new DataView(size.buffer).setBigUint64(0,BigInt(name.length));parts.push(size,name,digest(files[path]));}
  const framed=new Uint8Array(parts.reduce((sum,p)=>sum+p.length,0));let offset=0;
  for(const part of parts){framed.set(part,offset);offset+=part.length;}
  return Array.from(digest(framed),b=>b.toString(16).padStart(2,'0')).join('');
}
