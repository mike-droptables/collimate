import { useSyncExternalStore } from 'react';
// Ephemeral mount evidence only. Approval always uses the server's current package hash.
const mounts = new Map<string, {mountId:string | null; hash:string}>();
const listeners = new Set<() => void>();
const keyOf = (cell:string,path:string) => JSON.stringify([cell,path]);
if (typeof window !== 'undefined') window.addEventListener('collimate:renderer-preview', event => {
    const detail = (event as CustomEvent).detail;
    if (!detail || typeof detail.cellId !== 'string' || typeof detail.definitionPath !== 'string' || typeof detail.mountId !== 'string' || typeof detail.contentHash !== 'string' || typeof detail.active !== 'boolean') return;
    const key = keyOf(detail.cellId, detail.definitionPath);
    if (detail.active) {
        mounts.delete(key); mounts.set(key, {mountId:detail.mountId,hash:detail.contentHash});
        while (mounts.size > 128) mounts.delete(mounts.keys().next().value!);
    } else if (mounts.get(key)?.mountId === detail.mountId) mounts.set(key, {mountId:null, hash:mounts.get(key)!.hash});
    else return;
    listeners.forEach(listener => listener());
});
const subscribe = (listener:()=>void) => { listeners.add(listener); return () => {listeners.delete(listener);}; };
export function useRendererPreviewHash(cell:string,path:string) {
    return useSyncExternalStore(subscribe, () => mounts.get(keyOf(cell,path))?.hash ?? null, () => null);
}
