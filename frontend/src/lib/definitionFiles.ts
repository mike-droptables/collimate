import { loadAll, dump, CORE_SCHEMA } from 'js-yaml';
import type { VirtualFile, FileBody } from './types';

export type DefinitionKind = 'renderer' | 'generator' | 'runner' | 'handler' | 'options';
export function definitionKind(path: string): DefinitionKind | null {
    const ext = path.toLowerCase().split('.').pop();
    return ext === 'options_stack' ? 'options' : ['renderer', 'generator', 'runner', 'handler'].includes(ext ?? '') ? ext as DefinitionKind : null;
}
export function parseDefinition(text: string): Record<string, unknown> {
    // loadAll, not load: js-yaml 5's load throws on a source with no
    // document, and an empty or comment-only definition is no mapping either.
    const documents = loadAll(text, { schema: CORE_SCHEMA });
    if (documents.length > 1) throw new Error('The definition must be a single YAML document.');
    const value: unknown = documents[0];
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('The definition must be a YAML mapping.');
    // Definition metadata must also be JSON-shaped; recursive YAML aliases
    // cannot be edited as finite fields and must not crash the form renderer.
    try { JSON.stringify(value); } catch { throw new Error('Recursive YAML aliases are not supported in definitions.'); }
    return value as Record<string, unknown>;
}
export const definitionText = (value: Record<string, unknown>) => dump(value, { schema: CORE_SCHEMA });
export const folderOf = (path: string) => path.slice(0, Math.max(0, path.lastIndexOf('/')));
export const textFile = (name:string,content:string):FileBody => ({name,content,language:'yaml',file_revision:0,body_hash:null,encoding:'utf-8',writer_id:'',edit_seq:0});
export function replaceFiles(current: VirtualFile[], added: VirtualFile[], removed: ReadonlySet<string> = new Set()): VirtualFile[] {
    const names = new Set(added.map(f => f.name));
    return [...current.filter(f => !removed.has(f.name) && !names.has(f.name)), ...added];
}
export function previewPath(definitionPath: string, taken: Iterable<string>): string {
    const folder = folderOf(definitionPath);
    const parent = folderOf(folder);
    const name = folder.split('/').pop() || 'renderer';
    const prefix = parent ? parent + '/' : '';
    const names = new Set(taken);
    for (let index = 1; ; index++) {
        const candidate = `${prefix}${name}${index === 1 ? '' : '-' + index}.renderer_example`;
        if (!names.has(candidate)) return candidate;
    }
}
