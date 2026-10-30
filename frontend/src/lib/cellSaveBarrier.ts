/** Mounted cell chrome supplies explicit server-save barriers.
 * Local view handoffs use rendererFlush and FileDocument local durability. */
const cellSaveBarriers = new Map<string, Set<() => void | Promise<void>>>();
/** Register a cell's save barrier. Returns its deregistration. */
export function registerCellSaveBarrier(cellId: string, fn: () => void | Promise<void>): () => void {
  let set = cellSaveBarriers.get(cellId);
  if (!set) { set = new Set(); cellSaveBarriers.set(cellId, set); }
  set.add(fn);
  return () => {
    const cur = cellSaveBarriers.get(cellId);
    if (!cur) return;
    cur.delete(fn);
    if (cur.size === 0) cellSaveBarriers.delete(cellId);
  };
}

/** Await accepted saves for one mounted cell, or every mounted cell. */
export async function flushCellSaves(cellId?: string): Promise<void> {
  const sets = cellId !== undefined
    ? [cellSaveBarriers.get(cellId)].filter((s): s is Set<() => void | Promise<void>> => !!s)
    : Array.from(cellSaveBarriers.values());
  await Promise.all(sets.flatMap(set => Array.from(set).map(fn => fn())));
}
