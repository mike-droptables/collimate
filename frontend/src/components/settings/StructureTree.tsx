import { KeyRound } from 'lucide-react';
import type { KeystoreStructure } from '@/lib/keystoreStructure';

/** A custom entry's structure as the user recreates it: each group, each key
 *  under it, and which keys are secret. Keys only, never values. */
export function StructureTree({ structure, label = 'Structure' }: { structure: KeystoreStructure; label?: string }) {
    const node = (tree: KeystoreStructure, depth: number) => <ul role={depth ? 'group' : 'tree'} aria-label={depth ? undefined : label}
        className={depth ? 'ml-3 flex flex-col gap-0.5 border-l border-line pl-2' : 'flex flex-col gap-0.5'}>
        {Object.keys(tree).sort().map(name => {
            const value = tree[name];
            return typeof value === 'string'
                ? <li key={name} role="treeitem" aria-label={`${name}, ${value}`} className="flex items-center gap-1.5 font-mono text-[0.71875rem] text-ink-2">
                    <span>{name}</span>
                    {value === 'secret'
                        ? <span className="inline-flex items-center gap-0.5 text-[0.65625rem] uppercase tracking-wider text-teal"><KeyRound className="h-3 w-3" />secret</span>
                        : <span className="text-[0.65625rem] uppercase tracking-wider text-ink-3">public</span>}
                </li>
                : <li key={name} role="treeitem" aria-label={`${name}, group`} aria-expanded className="flex flex-col gap-0.5 font-mono text-[0.71875rem] text-ink">
                    <span>{name}</span>
                    {node(value, depth + 1)}
                </li>;
        })}
    </ul>;
    return node(structure, 0);
}
