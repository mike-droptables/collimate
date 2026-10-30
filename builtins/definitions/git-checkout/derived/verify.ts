import {parseJson, type VerifyInput, type Verdict} from 'collimate/verify';

// A Git checkout record: <folder>.git-checkout.json at the cell root, written
// by the Git Clone / Update and Git Push runners. Mirrors
// content/generators/_env_shared/repositories.py read_marker rule for rule;
// sdk/tests/test_git_checkout_handler.py pins the parity.
const CAP = 4096, ABOUT_CAP = 500, REMOTE_CAP = 512, KEYS = 'about,branch,commit,remote';
const BAD = new Set([' ', '~', '^', ':', '?', '*', '[', '\\']);

function utf8(text: string): number {
 let size = 0;
 for (const ch of text) {
  const c = ch.codePointAt(0)!;
  if (c >= 0xd800 && c <= 0xdfff) return Infinity;   // a lone surrogate has no UTF-8 form
  size += c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4;
 }
 return size;
}

const LABEL = '[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', HOST = `${LABEL}(?:\\.${LABEL})*`;
const SEG = '[A-Za-z0-9_.+%][A-Za-z0-9_.+%~-]{0,99}', USER = '[A-Za-z_][A-Za-z0-9_.-]{0,63}', PORT = '(?::([1-9][0-9]{0,4}))?';
const HTTPS = new RegExp(`^https://(${HOST})${PORT}((?:/${SEG}){1,32})$`);
const SSH = new RegExp(`^ssh://${USER}@(${HOST})${PORT}(/~(?:${USER})?)?((?:/${SEG}){1,32})$`);
const SCP = new RegExp(`^${USER}@(${HOST}):(/|~(?:${USER})?/)?(${SEG}(?:/${SEG}){0,31})$`);
const BAD_ESCAPE = /%(?![0-9A-F]{2})|%(?:2E|2F|5C|[01][0-9A-F]|7F)/;

function remoteOk(v: string): boolean {
 if ([...v].length > REMOTE_CAP) return false;
 let host: string, port: string | undefined, tilde: string | undefined, path: string, scheme = 'https';
 const h = HTTPS.exec(v), s = h ? null : SSH.exec(v), c = h || s ? null : SCP.exec(v);
 if (h) [, host, port, path] = h;
 else if (s) { [, host, port, tilde, path] = s; scheme = 'ssh'; }
 else if (c) {
  let lead: string | undefined, rest: string;
  [, host, lead, rest] = c;
  tilde = lead?.startsWith('~') ? lead : undefined;
  path = '/' + rest;
  scheme = 'scp';
 }
 else return false;
 if (host.length > 253) return false;
 if (port !== undefined && (Number(port) > 65535 || Number(port) === (scheme === 'https' ? 443 : 22))) return false;
 const segments = path.slice(1).split('/');
 if (segments.length + (tilde === undefined ? 0 : 1) > 32) return false;
 if (segments.some(seg => seg === '.' || seg === '..')) return false;
 return !BAD_ESCAPE.test(path);
}

function branchOk(name: string): boolean {
 if (!name || utf8(name) > 255) return false;
 for (const ch of name) {
  const c = ch.codePointAt(0)!;
  if (c < 32 || c === 127 || BAD.has(ch)) return false;
 }
 if (/^[-/]/.test(name) || /[/.]$/.test(name) || name === '@' || name === 'HEAD') return false;
 if (name.includes('//') || name.includes('..') || name.includes('@{')) return false;
 return !name.split('/').some(part => part.startsWith('.') || part.endsWith('.lock'));
}

const invalid = (reason: string): Verdict => ({valid: false, error: `Not a valid Git checkout record (${reason})`});

export default (input: VerifyInput): Verdict => {
 // Only a record at the cell root describes a checkout; a file of this type
 // inside a folder is that repository's own content.
 if (input.path.includes('/')) return {valid: true};
 if (typeof input.text !== 'string')
  return invalid(input.bytes && input.bytes.size > CAP ? 'larger than 4 KB' : 'not UTF-8 JSON');
 if (utf8(input.text) > CAP) return invalid('larger than 4 KB');
 let value: any;
 try { value = parseJson(input.text); } catch { return invalid('not UTF-8 JSON'); }
 if (value === null || typeof value !== 'object' || Array.isArray(value)
   || Object.keys(value).sort().join() !== KEYS)
  return invalid('not an object with exactly about, remote, branch and commit');
 const {about, remote, branch, commit} = value;
 if (typeof about !== 'string' || !about || [...about].length > ABOUT_CAP) return invalid('bad about');
 if (typeof remote !== 'string' || !remoteOk(remote)) return invalid('bad remote');
 if (typeof branch !== 'string' || !branchOk(branch)) return invalid('bad branch');
 if (typeof commit !== 'string' || !/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(commit)) return invalid('bad commit');
 return {valid: true};
};
