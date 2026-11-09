import { useEffect, useState } from 'react';
import { btn, formLabel } from '@/components/ui/formStyles';
import { fetchRendererOffline, freezeRendererOffline, type RendererOffline } from '@/lib/api/catalog';

/** What the offline line says about a renderer's import graph. */
function describe(state: RendererOffline): string {
    if (state.roots === 0) return 'Imports nothing from the module CDNs: works offline.';
    if (state.complete) return `Works offline: every module it imports is on this installation (${state.roots === 1 ? '1 root' : `${state.roots} roots`}).`;
    const missing = state.missing.length;
    return `${missing === 1 ? '1 module it imports is' : `${missing} modules it imports are`} not stored on this installation yet${state.truncated ? ' (its graph is larger than the app walks)' : ''}: it needs the network until they are.`;
}

/**
 * A renderer card's offline line: whether this installation holds the
 * renderer's whole import graph (the module mirror's snapshot and store),
 * loaded when the card opens. Adding a renderer stores its graph in the
 * background while the app is online; 'Store for offline use' does it now,
 * for a renderer added while offline.
 */
export function RendererOfflineLine({ rendererId }: { rendererId: string }) {
    const [state, setState] = useState<RendererOffline | null>(null);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        let live = true;
        fetchRendererOffline(rendererId).then(
            answer => { if (live) setState(answer); },
            e => { if (live) setError(e instanceof Error ? e.message : String(e)); },
        );
        return () => { live = false; };
    }, [rendererId]);
    const store = async () => {
        setBusy(true); setError('');
        try { setState(await freezeRendererOffline(rendererId)); }
        catch (e) { setError(e instanceof Error ? e.message : String(e)); }
        finally { setBusy(false); }
    };
    return <div data-renderer-offline="" className="flex flex-col gap-1">
        <span className={formLabel}>Offline</span>
        {state && <p className="text-[0.75rem] leading-[1.45] text-ink-2">{describe(state)}</p>}
        {state && !state.complete && state.missing.length > 0 && <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
            {state.missing.slice(0, 5).map(item => <li key={item.url} className="break-all font-mono text-[0.6875rem] text-ink-3">{item.url}</li>)}
        </ul>}
        {state && !state.complete && <button type="button" className={`${btn} self-start`} disabled={busy} onClick={() => void store()}>
            {busy ? 'Storing…' : 'Store for offline use'}
        </button>}
        {!state && !error && <p className="text-[0.75rem] text-ink-3">Checking…</p>}
        {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
    </div>;
}
