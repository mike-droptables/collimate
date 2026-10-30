import schemas from './definition-schemas.json';
import {parseYaml, validateSchema, matchGlob, type VerifyInput, type Verdict} from './prelude';

const relative = (path: unknown): path is string => typeof path === 'string' && !!path &&
 !path.startsWith('/') && !path.includes('\\') && !path.includes('\0') && !/^[A-Za-z]:/.test(path) &&
 path.split('/').every(part => !['', '.', '..', '_runs'].includes(part));
const has = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key);
const requirePath = (path: unknown) => {if (!relative(path)) throw Error(`Invalid package-relative path: ${path}`);};
function unique(values: string[], label: string) {
 if (new Set(values).size !== values.length) throw Error(`${label} must be unique`);
}
function resolve(base: string, path: string): string {
 if (!path || path.startsWith('/') || path.includes('\\') || path.includes('\0') || /^[A-Za-z]:/.test(path)) throw Error('Invalid relative root');
 const parts = base ? base.split('/') : [];
 for (const part of path.split('/')) {
  if (!part || part === '.') continue;
  if (part === '..') {if (!parts.length) throw Error('Root escapes the cell');parts.pop();}
  else parts.push(part);
 }
 return parts.join('/');
}

const scriptExtensions = /\.(pyw?|[cm]?js|jsx|tsx?|sh|bash|zsh|fish|ps1|bat|cmd|pl|rb|php|lua|exe|dll|so|wasm)$/i;
function skillNames(files: Record<string, unknown>, prefix: string): Set<string> {
 const names = new Set<string>();
 for (const [path, text] of Object.entries(files)) {
  if (!path.startsWith(prefix)) continue;
  const parts = path.slice(prefix.length).split('/');
  if (parts.length !== 2 || parts[1] !== 'SKILL.md') continue;
  names.add(parts[0].toLowerCase());
  if (typeof text !== 'string' || !text.startsWith('---')) continue;
  const end = text.indexOf('\n---', 3);
  if (end < 0) continue;
  try {
   const front = parseYaml(text.slice(3,end)) as any;
   if (typeof front?.name === 'string') names.add(front.name.trim().toLowerCase() || parts[0].toLowerCase());
  } catch { /* An inert current build cannot suppress its accepted predecessor. */ }
 }
 return names;
}
function addedSkills(input: VerifyInput): void {
 const files = Object.entries(input.package).filter(([path]) => path.startsWith('skills/'));
 if (files.length > 128) throw Error('Added skills exceed the 128-file bound');
 const reserved = skillNames(input.package, 'derived/skills/');
 const owners = new Map<string,string>();
 for (const name of skillNames(input.package, 'skills/')) {
  if (reserved.has(name)) throw Error('Added skill name collides with derived skills');
 }
 let size = 0;
 for (const [path, text] of files) {
  requirePath(path);
  const parts = path.split('/'), lower = parts.slice(1).map(part => part.toLowerCase());
  if (parts.length < 3 || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(parts[1])
      || !has(input.package, `skills/${parts[1]}/SKILL.md`)) throw Error('Added skills require skills/<name>/SKILL.md');
  if (lower.includes('derived') || lower.includes('scripts') || lower.at(-1) === 'verify.ts'
      || scriptExtensions.test(path)) throw Error('Added skills cannot contain derived content, verifiers or scripts');
  if (typeof text !== 'string' || text.includes('\0') || text.trimStart().startsWith('#!'))
   throw Error('Added skills must be nonexecutable UTF-8 text');
  for (const char of text) {
   const code = char.codePointAt(0)!;
   if (code >= 0xd800 && code <= 0xdfff) throw Error('Added skills must be UTF-8 text');
   size += code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
  }
  if (size > 1024*1024) throw Error('Added skills exceed the 1 MiB text bound');
  const localNames = new Set([parts[1].toLowerCase()]);
  if (parts.length === 3 && parts[2] === 'SKILL.md' && text.startsWith('---')) {
   const end = text.indexOf('\n---',3);
   if (end < 0) throw Error('Skill frontmatter is incomplete');
   const front = parseYaml(text.slice(3,end)) as any;
   if (front !== null && (typeof front !== 'object' || Array.isArray(front))) throw Error('Skill frontmatter must be a mapping');
   if (front?.name !== undefined && typeof front.name !== 'string') throw Error('Skill name must be text');
   if (typeof front?.name === 'string') localNames.add(front.name.trim().toLowerCase() || parts[1].toLowerCase());
  }
  if (parts.length === 3 && parts[2] === 'SKILL.md') {
   for (const name of localNames) {
    if (owners.has(name) && owners.get(name) !== parts[1]) throw Error('Added skill names must be unique within their handler');
    owners.set(name,parts[1]);
   }
  }
 }
}

/** Pure shipped-definition checks. Activation and cross-package approvals stay host-owned. */
export function verifyDefinition(kind: keyof typeof schemas, input: VerifyInput): Verdict {
 try {
  if (typeof input.text !== 'string') throw Error('Definition must be UTF-8 text');
  const value = parseYaml(input.text) as any;
  const verdict = validateSchema(schemas[kind], value);
  if (!verdict.valid) return verdict;
  if (kind !== 'options' && !value.name.trim()) throw Error('Definition name must be nonempty');
  if (kind === 'generator') {
   if (!value.summary.trim()) throw Error('Generator summary must be nonempty');
   for (const path of [value.entrypoint, value.main_config, ...(value.draft_files ?? []), ...(value.configs ?? []).map((c: any) => c.path)]) {
    requirePath(path);
    if (!has(input.package, path)) throw Error(`Declared package file is missing: ${path}`);
   }
   for (const path of value.required_files ?? []) {
    requirePath(path);
    if (path === value.main_config) throw Error('required_files must be supplemental to main_config');
   }
   unique((value.configs ?? []).map((c: any) => c.id), 'Config IDs');
   unique((value.config_folders ?? []).map((c: any) => c.name), 'Config folders');
   for (const folder of value.config_folders ?? []) {
    requirePath(folder.name);
    if (!/^[A-Za-z_][A-Za-z0-9._-]{0,63}$/.test(folder.name)) throw Error('Invalid config folder slot');
    if (!folder.description.trim()) throw Error('Config folder description must be nonempty');
    if (folder.sample) {
     requirePath(folder.sample);
     if (folder.sample !== 'samples' && !folder.sample.startsWith('samples/')) throw Error('Config folder sample must be under samples/');
     if (!Object.keys(input.package).some(path => path === folder.sample || path.startsWith(folder.sample+'/')))
      throw Error(`Config folder sample is missing: ${folder.sample}`);
    }
   }
   if ((value.prompt_required || value.prompt_show_if || value.prompt_required_if) && !value.prompt_intent) throw Error('Prompt conditions require prompt_intent');
  } else if (kind === 'runner') {
   for (const path of Object.keys(value.settings)) requirePath(path);
   for (const folder of value.folders ?? []) {requirePath(folder);if (folder.includes('/')) throw Error('Folder slots are single path components');}
  } else if (kind === 'handler') {
   addedSkills(input);
   const extensions = value.types.map((type: any) => type.ext.toLowerCase());
   unique(extensions, 'Handler extensions');
   unique(value.examples.map((example: any) => example.folder), 'Example folders');
   for (const example of value.examples) {
    requirePath(example.folder);
    const prefix = `examples/${example.folder}/`;
    if (!Object.keys(input.package).some(path => path.startsWith(prefix) && extensions.some((ext: string) => path.toLowerCase().endsWith(ext))))
     throw Error(`Example ${example.folder} needs a file of a covered type`);
   }
  } else {
   const base = input.path.includes('/') ? input.path.slice(0,input.path.lastIndexOf('/')) : '';
   for (const option of value.options) {
    if (!option.name.trim()) throw Error('Option name must be nonempty text');
    const root = option.root === undefined ? base : resolve(base, option.root);
    if (option.root !== undefined && !input.cell_paths.some(path => !root || path.startsWith(root+'/'))) throw Error(`Root ${option.root} does not exist`);
    // Evaluate every pattern even when an earlier one matched: an invalid
    // pattern cannot hide behind a successful short-circuit.
    const patterns = option.files.map((pattern: string) => {
     matchGlob('', pattern, root);
     return pattern;
    });
    if (!input.cell_paths.some(path => path !== input.path && patterns.some((pattern: string) => matchGlob(path,pattern,root))))
     throw Error(`Option ${option.name} matches no files`);
   }
  }
  return {valid:true};
 } catch (error) {return {valid:false,error:error instanceof Error ? error.message : 'Definition verification failed'};}
}

export {schemas as definitionSchemas};
