import {ensureLocalFileDocuments} from './fileDocuments';
/** Renderer serialization precedes document persistence. Lifetimes stay host-owned. */
export const FLUSH_DEADLINE_MS = 5000;
type Entry = { cellId: string | undefined; path: string; flush: () => void | Promise<void>; pending?: Promise<void> };
const entries = new Set<Entry>();
export function boundedFlush(operation: () => void | Promise<void>): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Still saving, try again in a moment.')), FLUSH_DEADLINE_MS);
    Promise.resolve().then(operation).then(() => { clearTimeout(timer); resolve(); }, error => { clearTimeout(timer); reject(error); });
  });
}
export function registerRendererFlusher(cellId: string | undefined, path: string, flush: Entry['flush']): () => void {
  const entry: Entry = {cellId, path, flush};
  entries.add(entry);
  return () => { entries.delete(entry); };
}
export async function flushRendererEditors(cellId?: string, path?: string): Promise<void> {
  await Promise.all([...entries].filter(entry => (cellId === undefined || entry.cellId === cellId) && (path === undefined || entry.path === path)).map(entry => {
    if (!entry.pending) {
      entry.pending = boundedFlush(entry.flush);
      const pending = entry.pending;
      void pending.then(() => { if (entry.pending === pending) entry.pending = undefined; }, () => { if (entry.pending === pending) entry.pending = undefined; });
    }
    return entry.pending;
  }));
}
const sandboxFlushes=new WeakMap<Window,Promise<void>>();
/** Source/origin checked separately from the shared host dispatcher. */
export function flushSandbox(target: Window, isCurrent: () => boolean = () => true): Promise<void> {
  const existing=sandboxFlushes.get(target);
  if(existing)return existing;
  const requestId = crypto.randomUUID();
  const pending = new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => { finish(); reject(new Error('Still saving, try again in a moment.')); }, FLUSH_DEADLINE_MS);
    const finish = () => { clearTimeout(timer); window.removeEventListener('message', answer); };
    const answer = (event: MessageEvent) => {
      if (event.source !== target || event.origin !== window.location.origin || event.data?.type !== 'FLUSH_DRAFT_ACK' || event.data.requestId !== requestId) return;
      finish();
      if (!isCurrent()) { reject(new Error('Renderer changed before acknowledging its save.')); return; }
      if (event.data.error) reject(new Error(String(event.data.error)));
      else resolve();
    };
    window.addEventListener('message', answer);
    target.postMessage({type:'FLUSH_DRAFT',requestId},window.location.origin);
  });
  sandboxFlushes.set(target,pending);
  const forget=()=>{if(sandboxFlushes.get(target)===pending)sandboxFlushes.delete(target);};
  void pending.then(forget,forget);
  return pending;
}


/** Channel replacement removes the whole active editor scene. */
export async function withFlushedEditors<T>(replace:()=>Promise<T>):Promise<T> {
  await boundedFlush(async()=>{await flushRendererEditors();await ensureLocalFileDocuments();});
  return replace();
}
