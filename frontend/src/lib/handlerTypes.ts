import {caseFold} from './unicodeCaseFold';

export interface HandlerTypes {
  types: Array<{ext: string; render_as?: string | null}>;
  clashes?: string[];
  disabled?: boolean;
}

export interface HandlerTypeRule<T extends HandlerTypes> {
  ext: string;
  handler: T | null;
  renderAs: string | null;
}

/** Retain inert winning suffixes: a clash must not fall through to a shorter type. */
export function handlerTypeRules<T extends HandlerTypes>(handlers: T[]): HandlerTypeRule<T>[] {
  const byExt = new Map<string, HandlerTypeRule<T>>();
  for (const handler of handlers) {
    if (handler.disabled || !Array.isArray(handler.types)) continue;
    const clashes = new Set(handler.clashes?.map(caseFold));
    for (const type of handler.types) {
      if (typeof type.ext !== 'string') continue;
      const ext = caseFold(type.ext);
      const available = !byExt.has(ext) && !clashes.has(ext);
      byExt.set(ext, {
        ext,
        handler: available ? handler : null,
        renderAs: available && typeof type.render_as === 'string' ? caseFold(type.render_as) : null,
      });
    }
  }
  return [...byExt.values()].sort((a, b) => b.ext.length - a.ext.length);
}

export function handlerTypeForPath<T extends {ext: string}>(rules: T[], path: string): T | undefined {
  const folded = caseFold(path);
  return rules.find(rule => folded.endsWith(rule.ext));
}

export function handlerForPath<T extends HandlerTypes>(handlers: T[], path: string): T | null {
  return handlerTypeForPath(handlerTypeRules(handlers), path)?.handler ?? null;
}
