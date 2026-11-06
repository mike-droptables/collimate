/** The structure of a custom Key manager entry, as collimate.keystore reads
 *  it: key names mapped to 'secret', 'public' or a nested group. A runner
 *  role whose kind is custom names the exact structure it reads; an entry
 *  fills it only with the same keys, nesting and secret flags. */
export type KeystoreStructure = { [key: string]: 'secret' | 'public' | KeystoreStructure };
export interface StructureLeaf { path: string; secret: boolean }

/** collimate.keystore.KEY_PATTERN, MAX_DEPTH and MAX_KEYS. */
export const KEY_PATTERN = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;
const MAX_DEPTH = 6;
const MAX_KEYS = 64;

const isGroup = (value: unknown): value is KeystoreStructure => !!value && typeof value === 'object' && !Array.isArray(value);

/** Every key of a structure as a dotted path and its flag, in canonical
 *  (sorted) order; [] for anything that is not a structure. */
export function structureLeaves(structure: unknown): StructureLeaf[] {
    const found: StructureLeaf[] = [];
    const walk = (node: unknown, prefix: string): void => {
        if (!isGroup(node)) return;
        for (const name of Object.keys(node).sort()) {
            const value = node[name];
            if (isGroup(value)) walk(value, `${prefix}${name}.`);
            else found.push({ path: `${prefix}${name}`, secret: value === 'secret' });
        }
    };
    walk(structure, '');
    return found;
}

/** Why ``structure`` is not one the keystore accepts, or null (the rules of
 *  collimate.keystore.check_structure). */
export function structureProblem(structure: unknown): string | null {
    let keys = 0;
    const walk = (node: unknown, path: string, depth: number): string | null => {
        if (!isGroup(node) || !Object.keys(node).length) return `${path || 'The structure'} needs at least one key.`;
        if (depth > MAX_DEPTH) return `${path} nests deeper than ${MAX_DEPTH} groups.`;
        for (const [name, value] of Object.entries(node)) {
            if (!KEY_PATTERN.test(name)) return `“${name}” is not a key name: start with a letter, then letters, numbers or underscores.`;
            const where = path ? `${path}.${name}` : name;
            if (isGroup(value)) { const inner = walk(value, where, depth + 1); if (inner) return inner; }
            else if (value === 'secret' || value === 'public') keys++;
            else return `${where} must be secret, public or a group.`;
        }
        return null;
    };
    const problem = walk(structure, '', 0);
    if (problem) return problem;
    if (keys > MAX_KEYS) return `A structure holds at most ${MAX_KEYS} keys.`;
    if (!structureLeaves(structure).some(leaf => leaf.secret)) return 'Mark at least one key secret: an entry holds private values.';
    return null;
}

/** Whether two structures are one: the same keys, nesting and secret flags. */
export function sameStructure(a: unknown, b: unknown): boolean {
    if (structureProblem(a) || structureProblem(b)) return false;
    return JSON.stringify(structureLeaves(a)) === JSON.stringify(structureLeaves(b));
}

/** The structure whose keys are ``leaves`` (dotted paths); throws with the
 *  reason when a path repeats, is both a key and a group, or the result is
 *  not a structure the keystore accepts. */
export function structureFromLeaves(leaves: StructureLeaf[]): KeystoreStructure {
    const tree: KeystoreStructure = {};
    for (const { path, secret } of leaves) {
        const parts = path.split('.');
        let node = tree;
        for (const part of parts.slice(0, -1)) {
            const child = node[part] ?? (node[part] = {});
            if (!isGroup(child)) throw new Error(`“${path}”: ${part} is a key and cannot hold others.`);
            node = child;
        }
        const last = parts[parts.length - 1];
        if (last in node) throw new Error(`“${path}” is named twice, or is both a key and a group.`);
        node[last] = secret ? 'secret' : 'public';
    }
    const problem = structureProblem(tree);
    if (problem) throw new Error(problem);
    return tree;
}

/** A nested value tree's text at a dotted path ('' when absent). */
export function valueAt(values: unknown, path: string): string {
    let node: unknown = values;
    for (const part of path.split('.')) node = isGroup(node) ? (node as Record<string, unknown>)[part] : undefined;
    return typeof node === 'string' ? node : '';
}

/** The nested value tree of ``entries`` (dotted path → text). */
export function valuesFromPaths(entries: [string, string][]): Record<string, unknown> {
    const tree: Record<string, unknown> = {};
    for (const [path, value] of entries) {
        const parts = path.split('.');
        let node = tree;
        for (const part of parts.slice(0, -1)) node = (node[part] ??= {}) as Record<string, unknown>;
        node[parts[parts.length - 1]] = value;
    }
    return tree;
}

/** A new custom entry the Key manager opens pre-shaped: the role's label as
 *  its name and the role's structure with every value empty. The user types
 *  the values and saves; nothing is written until then. */
export interface CustomEntryDraft { name: string; structure: KeystoreStructure }
