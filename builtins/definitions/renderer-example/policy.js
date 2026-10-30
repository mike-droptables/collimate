/** Preview paths are exact unencoded cell paths, never URLs or filesystem paths. */
export function cellPath(value) {
  if(typeof value!=='string'||!value||value.length>2048||value.includes('\\')||value.includes('\0')||/^[A-Za-z]:/.test(value))throw new Error('Invalid cell-relative path');
  if(value.split('/').some(part=>!part||part==='.'||part==='..'))throw new Error('Invalid cell-relative path');
  return value;
}
export function writeAllowed(path, packagePrefix, enabled, readOnly) {
  try{cellPath(path);}catch{return false;}
  return enabled&&!readOnly&&!path.startsWith(packagePrefix+'/')&&!/\.(renderer|generator|runner|handler|options_stack)$/i.test(path);
}
/**
 * The sealed frame's CSP. Images come from the API origin; the frame imports
 * the host's font stylesheet (`fontsUrl`, on the app origin), so that origin
 * may serve a stylesheet and fonts, and nothing else.
 */
export function previewPolicy(apiOrigin, fontsUrl) {
  const origin=new URL(apiOrigin).origin;
  if(!/^https?:/.test(origin))throw new Error('Preview requires an HTTP API origin');
  const fonts=new URL(fontsUrl).origin;
  if(!/^https?:/.test(fonts))throw new Error('Preview requires an HTTP font origin');
  const fontSources=fonts===origin?origin:`${origin} ${fonts}`;
  return `default-src 'none'; script-src 'unsafe-inline' data:; connect-src 'none'; img-src ${origin} data:; font-src ${fontSources} data:; style-src 'unsafe-inline' ${fonts}; base-uri 'none'; form-action 'none'`;
}
export function validMessage(event, frame, token) {
  const value=event.data;
  return event.source===frame.contentWindow&&event.origin==='null'&&value?.tag==='collimate-preview'&&value.token===token&&Number.isSafeInteger(value.id)&&value.id>=0;
}
